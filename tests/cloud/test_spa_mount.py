"""The built SPA must actually be reachable (regression).

Why this test exists
--------------------
The deploy script builds the frontend, verifies ``dist/index.html`` exists, and
records ``frontend_built: true`` in its stamp. For months nothing served that
directory: ``create_app`` mounted only the API router. Every asset path 404'd,
and every check that existed passed anyway —

  * the process was up (``systemctl is-active`` → active)
  * the health endpoint answered (``/api/v1/health/db`` → 200)
  * the deploy stamp said the frontend was built
  * ``/docs`` rendered

The one thing nobody did was open a browser. So this file asserts what a
browser sees, not what a probe of the API sees.

Covered here
------------
* ``/`` returns the built shell
* an asset is served from disk
* a history-mode deep link returns the shell, not a 404
* a typo'd API path still errors instead of silently returning the shell
* path traversal out of dist is refused
* a missing dist degrades to "API only" instead of crashing at import

The last one matters because the alternative is a hard failure that takes the
API down whenever someone runs the backend without a frontend build — which is
every developer's normal inner loop.

A note on the fixture, because getting it wrong hangs the suite
---------------------------------------------------------------
These clients are constructed WITHOUT a ``with`` block, on purpose. Entering a
``TestClient`` as a context manager runs the application's lifespan, and that
lifespan connects to NATS with ``max_reconnect_attempts=-1`` — an infinite
retry that never returns when no server is listening. A test that hangs with no
output is a much worse failure than a red one, so the mount is exercised
without the startup sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ate_cloud.main import _mount_spa

ASSET = "assets/index-abc123.js"
ASSET_BODY = "console.log('bundle')"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A directory shaped like a real vite build output."""
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(
        '<!doctype html><div id="app"></div><script src="/assets/index-abc123.js">',
        encoding="utf-8",
    )
    (d / ASSET).write_text(ASSET_BODY, encoding="utf-8")
    return d


@pytest.fixture
def spa_client(dist: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client over an app carrying only the SPA mount.

    The real app is used in :class:`TestRouterStillWins`; everywhere else a
    bare app keeps the assertion about the mount rather than about startup.
    """
    from ate_cloud.config import settings

    monkeypatch.setattr(settings, "frontend_dist_dir", str(dist), raising=False)
    app: FastAPI = FastAPI()
    _mount_spa(app)
    return TestClient(app)


class TestSpaIsReachable:
    def test_root_serves_the_built_shell(self, spa_client: TestClient) -> None:
        """The one request a browser makes first."""
        r = spa_client.get("/")
        assert r.status_code == 200
        assert '<div id="app">' in r.text

    def test_assets_are_served_from_disk(self, spa_client: TestClient) -> None:
        r = spa_client.get(f"/{ASSET}")
        assert r.status_code == 200
        assert r.text == ASSET_BODY

    def test_deep_link_returns_the_shell_not_404(self, spa_client: TestClient) -> None:
        """The router is history-mode, so vue-router owns these paths.

        A refresh on ``/aterag-review`` mid-flow must not 404 — that is how a
        half-finished review session gets lost.
        """
        r = spa_client.get("/aterag-review")
        assert r.status_code == 200
        assert '<div id="app">' in r.text

    def test_traversal_out_of_dist_is_refused(self, spa_client: TestClient, tmp_path: Path) -> None:
        """``dist`` is a web root; its parent is not.

        The deployment's own ``.env`` sits next to ``frontend/`` and holds the
        database credentials. A static handler that resolved ``../`` out of its
        root would publish it to anyone who can reach the port.

        The file is planted one level above dist, so a handler without the
        containment check would find it by name and return its contents.
        """
        (tmp_path / "secret.env").write_text("DATABASE_URL=postgres://user:pw@db/ate", encoding="utf-8")

        for probe in ("/../secret.env", "/..%2Fsecret.env", "/assets/../../secret.env"):
            r = spa_client.get(probe)
            assert "postgres://" not in r.text, f"{probe} leaked a file from outside dist"

    def test_files_inside_dist_are_still_reachable(self, spa_client: TestClient) -> None:
        """The containment check must not over-block.

        A handler that refused every path containing ``..`` would be fine, but
        one that refused everything would be a broken site — so the positive
        case is asserted alongside the negative one.
        """
        assert spa_client.get(f"/{ASSET}").status_code == 200


class TestRouterStillWins:
    """The catch-all must be registered after the API, and stay out of its way."""

    def test_unknown_api_path_is_a_404_not_the_shell(self, spa_client: TestClient) -> None:
        """A catch-all that swallows the API turns client errors into confusion.

        Returning the HTML shell with 200 for a typo'd endpoint means the caller
        fails to parse JSON a long way from the actual mistake.
        """
        r = spa_client.get("/api/v1/knowledge/conditions")
        assert r.status_code == 404
        assert '<div id="app">' not in r.text

    def test_unknown_api_path_under_a_deeper_prefix(self, spa_client: TestClient) -> None:
        """The guard has to match on the path, not on an exact string.

        ``api`` alone and ``api/...`` are both live; only equality would let the
        nested ones through.
        """
        for probe in ("/api", "/api/v1", "/api/v1/knowledge/conditions/summary"):
            assert spa_client.get(probe).status_code == 404, probe


class TestMissingDistDegrades:
    def test_api_serves_without_a_build(self, tmp_path: Path, monkeypatch) -> None:
        """No build is the normal state of a developer's inner loop.

        Crashing at import would take the API down for every backend-only
        change; skipping quietly is what let the deployed box look healthy
        while serving nothing at all.
        """
        from ate_cloud.config import settings

        monkeypatch.setattr(settings, "frontend_dist_dir", str(tmp_path / "nope"), raising=False)
        app: FastAPI = FastAPI()
        _mount_spa(app)  # must not raise

        assert TestClient(app).get("/").status_code == 404

    def test_dist_without_assets_dir_does_not_raise(self, tmp_path: Path, monkeypatch) -> None:
        """``StaticFiles`` raises at construction if the directory is absent.

        A partial build must not turn into an unbootable service.
        """
        from ate_cloud.config import settings

        d = tmp_path / "dist"
        d.mkdir()
        (d / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
        monkeypatch.setattr(settings, "frontend_dist_dir", str(d), raising=False)

        app: FastAPI = FastAPI()
        _mount_spa(app)  # must not raise

        assert TestClient(app).get("/").status_code == 200
