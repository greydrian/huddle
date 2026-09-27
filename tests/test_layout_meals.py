"""Input validation for the unauthenticated /api/layout and /api/meals
endpoints, plus per-connection SQLite pragmas."""

import pytest

from app import database


async def _layout(db):
    rows = await (await db.execute(
        "SELECT widget_id, grid_x, grid_y, grid_w, grid_h, is_visible FROM layout_state ORDER BY widget_id"
    )).fetchall()
    return [tuple(row) for row in rows]


async def _meals(db):
    rows = await (await db.execute("SELECT date, meal_description FROM meal_plans")).fetchall()
    return [tuple(row) for row in rows]


async def test_layout_save_round_trip(client, db):
    # The same shape dashboard.html's persistLayout() posts.
    items = [
        {"id": "tasks", "x": 1, "y": 9, "w": 5, "h": 3},
        {"id": "meals", "x": 10, "y": 0, "w": 2, "h": 4},
    ]
    resp = await client.post("/api/layout", json={"items": items})
    assert resp.status_code == 200

    rows = {r["widget_id"]: r for r in await (await db.execute("SELECT * FROM layout_state")).fetchall()}
    assert (rows["tasks"]["grid_x"], rows["tasks"]["grid_y"], rows["tasks"]["grid_w"], rows["tasks"]["grid_h"]) == (1, 9, 5, 3)
    assert (rows["meals"]["grid_x"], rows["meals"]["grid_w"]) == (10, 2)


GOOD = {"id": "tasks", "x": 0, "y": 0, "w": 4, "h": 4}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"{not json", id="bad-json"),
        pytest.param([GOOD], id="list-body"),
        pytest.param({}, id="missing-items"),
        pytest.param({"items": [{"id": "tasks", "x": 0, "y": 0, "w": 4}]}, id="missing-key"),
        pytest.param({"items": [{**GOOD, "x": "a"}]}, id="string-x"),
        pytest.param({"items": [{**GOOD, "x": "3"}]}, id="numeric-string-x"),
        pytest.param({"items": [{**GOOD, "x": 1.5}]}, id="float-x"),
        pytest.param({"items": [{**GOOD, "id": "nope"}]}, id="unknown-widget"),
        pytest.param({"items": [{**GOOD, "x": -1}]}, id="negative-x"),
        pytest.param({"items": [{**GOOD, "w": 0}]}, id="zero-w"),
        pytest.param({"items": [{**GOOD, "x": 10, "w": 4}]}, id="overflows-columns"),
        pytest.param({"items": [{**GOOD, "h": 100000}]}, id="huge-h"),
        # One bad item must not let the good one through either.
        pytest.param({"items": [{**GOOD, "x": 5}, {**GOOD, "id": "meals", "y": "a"}]}, id="partial"),
    ],
)
async def test_layout_rejects_malformed(client, db, body):
    before = await _layout(db)
    if isinstance(body, bytes):
        resp = await client.post("/api/layout", content=body, headers={"Content-Type": "application/json"})
    else:
        resp = await client.post("/api/layout", json=body)
    assert 400 <= resp.status_code < 500
    assert await _layout(db) == before


async def test_visibility_endpoint_is_gone(client, db):
    before = await _layout(db)
    resp = await client.post("/api/layout/tasks/visibility", json={"visible": False})
    assert resp.status_code in (404, 405)
    assert await _layout(db) == before


async def test_meal_valid_date_is_stored(client, db):
    resp = await client.post("/api/meals/2026-09-27", data={"description": "  Tacos  "})
    assert resp.status_code == 200
    assert await _meals(db) == [("2026-09-27", "Tacos")]


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-01", "2026-02-30", "27-09-2026"])
async def test_meal_invalid_date_rejected(client, db, bad):
    resp = await client.post(f"/api/meals/{bad}", data={"description": "Tacos"})
    assert 400 <= resp.status_code < 500
    assert await _meals(db) == []


async def test_get_db_sets_synchronous_normal():
    async with database.get_db() as conn:
        (value,) = await (await conn.execute("PRAGMA synchronous")).fetchone()
    assert value == 1  # NORMAL
