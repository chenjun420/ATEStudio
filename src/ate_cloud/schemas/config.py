"""Pydantic schemas for worker configuration distribution.

Defines request/response models for the config distribution API endpoints.
Config values are stored as UTF-8 strings in the JetStream KV bucket
``ate-configs`` with key pattern ``workers.{worker_id}.{config_key}``.
"""

from pydantic import BaseModel, Field


class ConfigUpdate(BaseModel):
    """Single config key-value update for a worker.

    Attributes:
        key: Configuration key (e.g., "instrument.oscilloscope.sample_rate").
        value: Configuration value as a string (JSON-encoded if structured).
    """

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1)


class ConfigBatchUpdate(BaseModel):
    """Batch config update for a worker.

    Attributes:
        configs: Mapping of config keys to string values.
    """

    configs: dict[str, str] = Field(..., min_length=1)


class ConfigResponse(BaseModel):
    """Response for a single config key.

    Attributes:
        key: Configuration key (without the worker prefix).
        value: Configuration value.
        revision: KV revision number of the latest put.
    """

    key: str
    value: str
    revision: int


class ConfigListResponse(BaseModel):
    """Response listing all config keys for a worker.

    Attributes:
        worker_id: The worker identifier.
        configs: Mapping of all config keys to values.
    """

    worker_id: str
    configs: dict[str, str]
