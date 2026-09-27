async def test_static_files_must_revalidate(client):
    resp = await client.get("/static/css/style.css")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers.get("etag")


async def test_unchanged_static_file_is_a_cheap_304(client):
    etag = (await client.get("/static/css/style.css")).headers["etag"]

    resp = await client.get("/static/css/style.css", headers={"If-None-Match": etag})

    assert resp.status_code == 304
    assert resp.headers["cache-control"] == "no-cache"
