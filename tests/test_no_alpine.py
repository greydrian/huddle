"""Alpine.js was vendored but never used, so it was removed. Keep it gone:
nothing in the dashboard should load it (or reference the old banner)."""


async def test_dashboard_loads_no_alpine_script(db, client):
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text.lower()
    assert "alpine" not in html
    assert "notification-banner" not in html
