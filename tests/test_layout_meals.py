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
    assert resp.json() == {"status": "ok", "saved": 2, "skipped": 0}

    rows = {r["widget_id"]: r for r in await (await db.execute("SELECT * FROM layout_state")).fetchall()}
    assert (rows["tasks"]["grid_x"], rows["tasks"]["grid_y"], rows["tasks"]["grid_w"], rows["tasks"]["grid_h"]) == (1, 9, 5, 3)
    assert (rows["meals"]["grid_x"], rows["meals"]["grid_w"]) == (10, 2)


async def test_layout_accepts_real_seeded_rows(client, db):
    """Regression: whatever the dashboard renders must round-trip, or every
    drag would silently fail to save."""
    rows = await (await db.execute("SELECT * FROM layout_state WHERE is_visible = 1")).fetchall()
    items = [
        {"id": r["widget_id"], "x": r["grid_x"], "y": r["grid_y"], "w": r["grid_w"], "h": r["grid_h"],
         "extra": "ignored"}
        for r in rows
    ]
    before = await _layout(db)
    resp = await client.post("/api/layout", json={"items": items})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "saved": len(items), "skipped": 0}
    assert await _layout(db) == before


GOOD = {"id": "tasks", "x": 0, "y": 0, "w": 4, "h": 4}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"{not json", id="bad-json"),
        pytest.param([GOOD], id="list-body"),
        pytest.param({}, id="missing-items"),
        pytest.param({"items": GOOD}, id="items-not-a-list"),
        pytest.param({"items": ["tasks", 3]}, id="items-not-objects"),
    ],
)
async def test_layout_rejects_wrong_shape(client, db, body):
    before = await _layout(db)
    if isinstance(body, bytes):
        resp = await client.post("/api/layout", content=body, headers={"Content-Type": "application/json"})
    else:
        resp = await client.post("/api/layout", json=body)
    assert resp.status_code == 422
    assert await _layout(db) == before


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"id": "meals", "x": 0, "y": 0, "w": 4}, id="missing-key"),
        pytest.param({**GOOD, "id": "meals", "x": "a"}, id="string-x"),
        pytest.param({**GOOD, "id": "meals", "x": "3"}, id="numeric-string-x"),
        pytest.param({**GOOD, "id": "meals", "x": 1.5}, id="float-x"),
        pytest.param({**GOOD, "id": "meals", "x": True}, id="bool-x"),
        pytest.param({**GOOD, "id": "nope"}, id="unknown-widget"),
    ],
)
async def test_bad_item_is_skipped_and_good_ones_saved(client, db, bad):
    before = {row[0]: row for row in await _layout(db)}
    resp = await client.post("/api/layout", json={"items": [{**GOOD, "x": 5, "y": 20}, bad]})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "saved": 1, "skipped": 1}

    after = {row[0]: row for row in await _layout(db)}
    assert after["tasks"][1:5] == (5, 20, 4, 4)
    assert after["meals"] == before["meals"]
    assert "nope" not in after


@pytest.mark.parametrize(
    "item, expected",
    [
        pytest.param({**GOOD, "x": -1}, (0, 0, 4, 4), id="negative-x"),
        pytest.param({**GOOD, "w": 0}, (0, 0, 1, 4), id="zero-w"),
        pytest.param({**GOOD, "x": 10, "w": 4}, (8, 0, 4, 4), id="overflows-columns"),
        pytest.param({**GOOD, "w": 40}, (0, 0, 12, 4), id="too-wide"),
        pytest.param({**GOOD, "h": 100000}, (0, 0, 4, 500), id="huge-h"),
        pytest.param({**GOOD, "y": 499, "h": 4}, (0, 496, 4, 4), id="y-plus-h-past-max"),
    ],
)
async def test_out_of_range_values_are_clamped(client, db, item, expected):
    resp = await client.post("/api/layout", json={"items": [item]})
    assert resp.status_code == 200
    assert resp.json()["skipped"] == 0
    after = {row[0]: row for row in await _layout(db)}
    assert after["tasks"][1:5] == expected


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
