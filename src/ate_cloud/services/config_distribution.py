"""Config distribution service — manages worker config in JetStream KV.

Stores per-worker configuration in the ``ate-configs`` JetStream KV bucket
with key pattern ``workers.{worker_id}.{config_key}``. Configuration is
persistent (no TTL) and can be watched by edge workers via
:class:`~ate_platform.scheduler.config_watcher.ConfigWatcher` for real-time
updates.

Per AGENTS.md section 7: if the KV bucket is unavailable, operations raise
``RuntimeError`` — no silent degradation.

Standalone FastAPI endpoints (PUT/GET) are defined at the bottom of this
module as an :class:`~fastapi.APIRouter`. They are NOT wired into the main
router — defer to the T9 batch for route registration.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from nats.aio.client import Client as NatsClient
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError

from ate_cloud.schemas.config import (
    ConfigBatchUpdate,
    ConfigListResponse,
    ConfigResponse,
    ConfigUpdate,
)

logger = logging.getLogger(__name__)

# KV bucket for configuration distribution (persistent, no TTL).
# Separate from the ``ate-workers`` heartbeat bucket which has a 30s TTL.
CONFIG_KV_BUCKET: str = "ate-configs"

# Key prefix for worker configs: workers.{worker_id}.{config_key}
_WORKER_KEY_PREFIX: str = "workers"


def _worker_config_key(worker_id: str, config_key: str) -> str:
    """Build the KV key for a worker's config entry."""
    return f"{_WORKER_KEY_PREFIX}.{worker_id}.{config_key}"


def _worker_config_prefix(worker_id: str) -> str:
    """Build the KV key prefix for all of a worker's configs."""
    return f"{_WORKER_KEY_PREFIX}.{worker_id}."


class ConfigDistributionService:
    """Manages worker configuration in the ``ate-configs`` JetStream KV bucket.

    Configuration keys follow the pattern ``workers.{worker_id}.{config_key}``.
    Values are stored as UTF-8 encoded bytes. The bucket is persistent (no
    TTL) — configs survive NATS restarts.
    """

    def __init__(self, nats_client: NatsClient) -> None:
        self._nc = nats_client
        self._kv: Any = None

    async def _get_kv(self) -> Any:
        """Get or create the ``ate-configs`` KV bucket handle.

        If the bucket does not exist, it is created (persistent, no TTL).

        Raises:
            RuntimeError: If the KV bucket cannot be accessed or created.
        """
        if self._kv is not None:
            return self._kv
        js = self._nc.jetstream()
        try:
            self._kv = await js.key_value(CONFIG_KV_BUCKET)
        except NotFoundError:
            try:
                self._kv = await js.create_key_value(bucket=CONFIG_KV_BUCKET)
                logger.info("Created KV bucket '%s' (persistent, no TTL)", CONFIG_KV_BUCKET)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create KV bucket '{CONFIG_KV_BUCKET}': {e}"
                ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to access KV bucket '{CONFIG_KV_BUCKET}': {e}"
            ) from e
        return self._kv

    async def ensure_bucket(self) -> None:
        """Ensure the ``ate-configs`` KV bucket exists.

        Must be called at startup (e.g., from lifespan) before any config
        operations. Per AGENTS.md section 7, creation failure is fatal.

        Raises:
            RuntimeError: If bucket creation fails.
        """
        await self._get_kv()

    async def put_config(self, worker_id: str, key: str, value: str) -> int:
        """Put a single config value for a worker.

        Args:
            worker_id: Unique worker identifier.
            key: Configuration key (e.g., "instrument.sample_rate").
            value: Configuration value as a string.

        Returns:
            The KV revision number of the put operation.

        Raises:
            RuntimeError: If the KV bucket is unavailable or the put fails.
        """
        kv = await self._get_kv()
        full_key = _worker_config_key(worker_id, key)
        payload = value.encode("utf-8")
        try:
            revision: int = int(await kv.put(full_key, payload))
        except Exception as e:
            raise RuntimeError(
                f"Failed to put config '{key}' for worker '{worker_id}': {e}"
            ) from e
        logger.info(
            "Put config for worker '%s' (key=%s, rev=%s)",
            worker_id, full_key, revision,
        )
        return revision

    async def get_config(self, worker_id: str, key: str) -> str | None:
        """Get a single config value for a worker.

        Args:
            worker_id: Unique worker identifier.
            key: Configuration key.

        Returns:
            The config value as a string, or ``None`` if the key doesn't exist.

        Raises:
            RuntimeError: If the KV bucket is unavailable (not if the key
                is missing — that returns ``None``).
        """
        kv = await self._get_kv()
        full_key = _worker_config_key(worker_id, key)
        try:
            entry = await kv.get(full_key)
        except KeyNotFoundError:
            return None
        except Exception as e:
            raise RuntimeError(
                f"Failed to get config '{key}' for worker '{worker_id}': {e}"
            ) from e
        return entry.value.decode("utf-8") if entry.value else ""

    async def get_all_config(self, worker_id: str) -> dict[str, str]:
        """Get all config values for a worker.

        Args:
            worker_id: Unique worker identifier.

        Returns:
            Mapping of config keys (without the worker prefix) to values.
            Returns an empty dict if the worker has no configs.

        Raises:
            RuntimeError: If the KV bucket is unavailable.
        """
        kv = await self._get_kv()
        prefix = _worker_config_prefix(worker_id)
        configs: dict[str, str] = {}
        try:
            keys = await kv.keys()
        except NoKeysError:
            return configs
        except Exception as e:
            raise RuntimeError(
                f"Failed to list keys for worker '{worker_id}': {e}"
            ) from e
        for full_key in keys:
            if not full_key.startswith(prefix):
                continue
            config_key = full_key[len(prefix):]
            try:
                entry = await kv.get(full_key)
            except KeyNotFoundError:
                continue  # Key was deleted between list and get
            configs[config_key] = entry.value.decode("utf-8") if entry.value else ""
        return configs

    async def put_batch(self, worker_id: str, configs: dict[str, str]) -> list[int]:
        """Put multiple config values for a worker.

        Each key-value pair is put sequentially into the KV bucket. NATS KV
        does not support atomic multi-key transactions, so a failure mid-batch
        leaves prior puts applied.

        Args:
            worker_id: Unique worker identifier.
            configs: Mapping of config keys to values.

        Returns:
            List of KV revision numbers, one per key (in iteration order).

        Raises:
            RuntimeError: If the KV bucket is unavailable or any put fails.
        """
        kv = await self._get_kv()
        revisions: list[int] = []
        for key, value in configs.items():
            full_key = _worker_config_key(worker_id, key)
            payload = value.encode("utf-8")
            try:
                revision: int = int(await kv.put(full_key, payload))
            except Exception as e:
                raise RuntimeError(
                    f"Failed to put config '{key}' for worker '{worker_id}': {e}"
                ) from e
            revisions.append(revision)
        logger.info(
            "Put %d config entries for worker '%s'",
            len(configs), worker_id,
        )
        return revisions


# ---------------------------------------------------------------------------
# Standalone API endpoints — NOT wired into the main router (defer to T9).
# Import and include this router in router.py when ready:
#
#     from ate_cloud.services.config_distribution import config_router
#     api_router.include_router(config_router, prefix="/workers", tags=["workers"])
# ---------------------------------------------------------------------------

config_router = APIRouter()


def _get_config_service(request: Request) -> ConfigDistributionService:
    """Dependency: extract or create ConfigDistributionService from app state.

    The NATS client is expected on ``app.state.nc`` (set by the lifespan).
    """
    nc = getattr(request.app.state, "nc", None)
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not available",
        )
    return ConfigDistributionService(nc)


@config_router.put(
    "/{worker_id}/config/{key}",
    response_model=ConfigResponse,
    status_code=status.HTTP_200_OK,
)
async def put_worker_config(
    worker_id: str,
    key: str,
    update: ConfigUpdate,
    service: Annotated[ConfigDistributionService, Depends(_get_config_service)],
) -> ConfigResponse:
    """PUT /api/v1/workers/{worker_id}/config/{key} — update a single config.

    The ``key`` path parameter and the ``key`` field in the request body must
    match. The value is stored in the ``ate-configs`` KV bucket.
    """
    if key != update.key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path key '{key}' does not match body key '{update.key}'",
        )
    try:
        revision = await service.put_config(worker_id, key, update.value)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return ConfigResponse(key=key, value=update.value, revision=revision)


@config_router.put(
    "/{worker_id}/config",
    response_model=list[ConfigResponse],
    status_code=status.HTTP_200_OK,
)
async def put_worker_config_batch(
    worker_id: str,
    batch: ConfigBatchUpdate,
    service: Annotated[ConfigDistributionService, Depends(_get_config_service)],
) -> list[ConfigResponse]:
    """PUT /api/v1/workers/{worker_id}/config — batch update configs.

    All key-value pairs in the request body are stored in the ``ate-configs``
    KV bucket. Returns one response per key, with the KV revision number.
    """
    try:
        revisions = await service.put_batch(worker_id, batch.configs)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return [
        ConfigResponse(key=k, value=v, revision=r)
        for (k, v), r in zip(batch.configs.items(), revisions, strict=True)
    ]


@config_router.get(
    "/{worker_id}/config/{key}",
    response_model=ConfigResponse,
)
async def get_worker_config(
    worker_id: str,
    key: str,
    service: Annotated[ConfigDistributionService, Depends(_get_config_service)],
) -> ConfigResponse:
    """GET /api/v1/workers/{worker_id}/config/{key} — fetch a single config."""
    try:
        value = await service.get_config(worker_id, key)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config key '{key}' not found for worker '{worker_id}'",
        )
    return ConfigResponse(key=key, value=value, revision=0)


@config_router.get(
    "/{worker_id}/config",
    response_model=ConfigListResponse,
)
async def get_all_worker_config(
    worker_id: str,
    service: Annotated[ConfigDistributionService, Depends(_get_config_service)],
) -> ConfigListResponse:
    """GET /api/v1/workers/{worker_id}/config — fetch all configs for a worker."""
    try:
        configs = await service.get_all_config(worker_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return ConfigListResponse(worker_id=worker_id, configs=configs)
