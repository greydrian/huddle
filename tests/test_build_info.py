"""The running build's id (app/build_info.py): the wall reloads itself
after a deploy when /api/rev's build differs from the page's."""

from app import build_info


def _tree(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_the_same_tree_always_gives_the_same_id(tmp_path):
    _tree(tmp_path, {"main.py": b"print(1)", "static/js/fresh.js": b"var a;", "templates/base.html": b"<html>"})
    first = build_info.compute(tmp_path)
    assert first == build_info.compute(tmp_path)
    assert len(first) == 12 and all(c in "0123456789abcdef" for c in first)


def test_any_change_to_code_templates_or_static_changes_it(tmp_path):
    _tree(tmp_path, {"main.py": b"print(1)", "static/js/fresh.js": b"var a;", "templates/base.html": b"<html>"})
    before = build_info.compute(tmp_path)
    for name, content in (
        ("static/js/fresh.js", b"var b;"),
        ("templates/base.html", b"<html lang=en>"),
        ("main.py", b"print(2)"),
        ("static/css/new.css", b"a{}"),
    ):
        _tree(tmp_path, {name: content})
        after = build_info.compute(tmp_path)
        assert after != before, name
        before = after


def test_a_renamed_file_changes_it(tmp_path):
    _tree(tmp_path, {"a.py": b"x"})
    before = build_info.compute(tmp_path)
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    assert build_info.compute(tmp_path) != before


def test_bytecode_caches_are_ignored(tmp_path):
    """A restart writes __pycache__ (and the image may carry .pyc files):
    that must not make the wall reload."""
    _tree(tmp_path, {"main.py": b"print(1)"})
    before = build_info.compute(tmp_path)
    _tree(
        tmp_path,
        {"__pycache__/main.cpython-314.pyc": b"\x00", "services/__pycache__/x.pyc": b"\x01", "stray.pyc": b"\x02"},
    )
    assert build_info.compute(tmp_path) == before


def test_the_real_app_has_a_build_id():
    assert build_info.build_id() == build_info.compute()


async def test_rev_health_and_dashboard_report_the_same_build(client):
    build = build_info.build_id()
    assert (await client.get("/api/rev")).json()["build"] == build
    assert (await client.get("/health")).json()["build"] == build
    html = (await client.get("/")).text
    assert f'data-build="{build}"' in html
