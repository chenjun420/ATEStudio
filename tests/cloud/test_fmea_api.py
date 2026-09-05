"""Task 13 — FMEA CRUD API tests (server-computed RPN).

Exercises the REST surface for the task-10 ``FMEA`` ORM model under
``/api/v1/fmea``:

- POST creates an entry and returns the SERVER-derived rpn = S*O*D.
- A client-supplied ``rpn`` is ignored and recomputed (never trusted).
- severity/occurrence/detection outside 1-10 -> HTTP 422 (Pydantic boundary).
- GET list supports optional component_code / fault_code filters.
- GET one returns 404 for a missing id.
- PUT partial update recomputes rpn when a rating changes.
- DELETE removes the entry; a second delete is 404 (matches products router).

The mount-level JWT guard (anonymous -> 401) is owned by
``test_auth_enforcement.py``; here the conftest dev-mode bypass applies.
"""

from __future__ import annotations

from typing import Any

import pytest


def _fmea_payload(
    *,
    component_code: str = "PSU_MAIN",
    fault_code: str | None = "over_voltage",
    severity: int = 7,
    occurrence: int = 4,
    detection: int = 3,
    **extra: Any,
) -> dict[str, Any]:
    """Build a valid FMEA create payload (S*O*D = 84 by default)."""
    payload: dict[str, Any] = {
        "component_code": component_code,
        "fault_code": fault_code,
        "failure_mode": "Output over-voltage",
        "effects": "OVP trips, UUT reset",
        "cause": "Feedback resistor drift",
        "severity": severity,
        "occurrence": occurrence,
        "detection": detection,
        "recommended_action": "Replace R12 feedback divider",
    }
    payload.update(extra)
    return payload


# ── Create ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_201_with_server_computed_rpn(client: Any) -> None:
    """Given valid S=7/O=4/D=3, POST returns 201 and rpn == 84 (7*4*3)."""
    resp = await client.post("/api/v1/fmea", json=_fmea_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["rpn"] == 84
    assert data["severity"] == 7
    assert data["occurrence"] == 4
    assert data["detection"] == 3
    assert data["id"]
    assert data["component_code"] == "PSU_MAIN"
    assert data["failure_mode"] == "Output over-voltage"
    assert "created_at" in data and "updated_at" in data


@pytest.mark.asyncio
async def test_client_supplied_rpn_is_ignored_and_recomputed(client: Any) -> None:
    """A body that includes a bogus rpn must not persist it: the server
    derives rpn = S*O*D regardless (and the field is not even an input)."""
    resp = await client.post(
        "/api/v1/fmea", json=_fmea_payload(rpn=99999)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["rpn"] == 84

    # The persisted row also carries the derived value, not the client's.
    list_resp = await client.get("/api/v1/fmea")
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["rpn"] == 84


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("severity", 0),
        ("severity", 11),
        ("occurrence", 0),
        ("occurrence", 42),
        ("detection", -1),
        ("detection", 100),
    ],
)
async def test_out_of_range_rating_returns_422(
    client: Any, field: str, bad_value: int
) -> None:
    """A rating outside [1, 10] is rejected at the boundary with HTTP 422."""
    resp = await client.post("/api/v1/fmea", json=_fmea_payload(**{field: bad_value}))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_missing_required_field_returns_422(client: Any) -> None:
    """Omitting a required rating yields 422 (parse boundary)."""
    payload = _fmea_payload()
    del payload["severity"]
    resp = await client.post("/api/v1/fmea", json=payload)
    assert resp.status_code == 422


# ── Read / list ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_items_and_total(client: Any) -> None:
    """GET list returns created entries with an envelope {items,total}."""
    await client.post("/api/v1/fmea", json=_fmea_payload(component_code="PSU_MAIN"))
    await client.post(
        "/api/v1/fmea", json=_fmea_payload(component_code="DMM_CH1", fault_code=None)
    )

    resp = await client.get("/api/v1/fmea")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert all("rpn" in item for item in body["items"])


@pytest.mark.asyncio
async def test_list_filter_by_component_code(client: Any) -> None:
    """The component_code query filter narrows results to that component."""
    await client.post("/api/v1/fmea", json=_fmea_payload(component_code="PSU_MAIN"))
    await client.post("/api/v1/fmea", json=_fmea_payload(component_code="DMM_CH1"))

    resp = await client.get("/api/v1/fmea", params={"component_code": "PSU_MAIN"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["component_code"] == "PSU_MAIN"


@pytest.mark.asyncio
async def test_list_filter_by_fault_code(client: Any) -> None:
    """The fault_code query filter narrows results to that fault."""
    await client.post(
        "/api/v1/fmea", json=_fmea_payload(fault_code="over_voltage")
    )
    await client.post(
        "/api/v1/fmea", json=_fmea_payload(fault_code="signal_loss", severity=5)
    )

    resp = await client.get("/api/v1/fmea", params={"fault_code": "signal_loss"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["fault_code"] == "signal_loss"


@pytest.mark.asyncio
async def test_get_one_returns_entry(client: Any) -> None:
    """GET /{id} returns the entry with its computed rpn."""
    created = (await client.post("/api/v1/fmea", json=_fmea_payload())).json()

    resp = await client.get(f"/api/v1/fmea/{created['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == created["id"]
    assert resp.json()["rpn"] == 84


@pytest.mark.asyncio
async def test_get_missing_id_returns_404(client: Any) -> None:
    """GET /{id} for an unknown id returns 404."""
    resp = await client.get("/api/v1/fmea/does-not-exist")
    assert resp.status_code == 404


# ── Update ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_recomputes_rpn_when_rating_changes(client: Any) -> None:
    """PUT with a new severity recomputes rpn server-side (7*4*3=84 -> 10*4*3=120)."""
    created = (await client.post("/api/v1/fmea", json=_fmea_payload())).json()
    assert created["rpn"] == 84

    resp = await client.put(f"/api/v1/fmea/{created['id']}", json={"severity": 10})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["severity"] == 10
    assert data["rpn"] == 120

    # A fresh GET confirms the recomputed value persisted.
    again = await client.get(f"/api/v1/fmea/{created['id']}")
    assert again.json()["rpn"] == 120


@pytest.mark.asyncio
async def test_update_ignores_client_rpn(client: Any) -> None:
    """A rpn field in a PUT body is ignored; rpn stays derived from ratings."""
    created = (await client.post("/api/v1/fmea", json=_fmea_payload())).json()

    resp = await client.put(
        f"/api/v1/fmea/{created['id']}", json={"rpn": 1, "occurrence": 5}
    )
    assert resp.status_code == 200, resp.text
    # 7 * 5 * 3 = 105, never the client's 1.
    assert resp.json()["rpn"] == 105


@pytest.mark.asyncio
async def test_update_missing_id_returns_404(client: Any) -> None:
    """PUT on an unknown id returns 404."""
    resp = await client.put("/api/v1/fmea/nope", json={"severity": 2})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_out_of_range_rating_returns_422(client: Any) -> None:
    """A partial update with an out-of-range rating is rejected with 422."""
    created = (await client.post("/api/v1/fmea", json=_fmea_payload())).json()
    resp = await client.put(f"/api/v1/fmea/{created['id']}", json={"detection": 11})
    assert resp.status_code == 422


# ── Delete ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_entry(client: Any) -> None:
    """DELETE removes the entry (204) and a subsequent GET is 404."""
    created = (await client.post("/api/v1/fmea", json=_fmea_payload())).json()

    resp = await client.delete(f"/api/v1/fmea/{created['id']}")
    assert resp.status_code == 204

    assert (await client.get(f"/api/v1/fmea/{created['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_id_returns_404(client: Any) -> None:
    """DELETE on an unknown id returns 404 (consistent with products router)."""
    resp = await client.delete("/api/v1/fmea/nope")
    assert resp.status_code == 404
