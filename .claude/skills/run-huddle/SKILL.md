---
name: run-huddle
description: Launch the Huddle dashboard and verify a change in a real browser — either against an isolated throwaway instance (default, safe for any change) or read-only against the family's live Docker instance. Use when asked to run, screenshot, or visually verify the app, or before calling a UI change done.
---

# Running and verifying Huddle

Two instances exist. **Default to the isolated one**; the live one holds the family's real data and a real Google connection — anything that writes there (adding a task, ticking an item) also syncs to their real Google Tasks.

## Isolated instance (safe for anything)
```bash
PY="C:\Users\Gonzoadmin\OneDrive\Documents\VSCode\Huddle\huddle\venv\Scripts\python.exe"
PORT=8011   # 8011-8019; check free first: netstat -ano | grep ":$PORT"
TMP=$(mktemp -d)
DATA_DIR="$TMP" "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port $PORT > "$TMP/server.log" 2>&1 &
for i in $(seq 1 20); do curl -sf http://127.0.0.1:$PORT/health >/dev/null && break; sleep 1; done
```
Run from the checkout/worktree whose code you want to test (uvicorn imports `app` from the current directory). Fresh DB: family seeded (Mum, Dad, Riley, Jamie), admin PIN `1234`, Google not connected. Stop it by PID: `netstat -ano | grep ":$PORT" | grep LISTENING` → `taskkill //PID <pid> //F`.

## Live instance (read-only checks)
`docker compose up -d --build` from the main checkout (needed after `requirements.txt` changes; code changes hot-reload via the bind mount). Serves on `http://localhost:8000`. If Docker isn't responding, Docker Desktop has stopped — relaunch `"/c/Users/Gonzoadmin/AppData/Local/Programs/DockerDesktop/Docker Desktop.exe"` and poll `docker info`. Inspect state with `docker compose exec -T family-display python -c "..."` using `app.database.get_db()`. Never run `docker compose config` (prints `.env` secrets).

## Driving it with Playwright
Write the script to the session scratchpad, not the repo, and run it with `"$PY"`:
```python
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8011"
with sync_playwright() as p:
    page = p.chromium.launch(args=["--no-sandbox"]).new_page(viewport={"width": 1300, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{BASE}/admin/login"); page.fill('input[name="pin"]', "1234"); page.click('button[type="submit"]')
    page.wait_for_selector("text=Family Members")
    page.goto(f"{BASE}/"); page.wait_for_selector("#widget-calendar")
    page.screenshot(path="dash.png")
    print("page errors:", errors)
```
Then **look at the screenshot** (Read the PNG) — passing selectors don't prove it looks right.

## Gotchas (all hit for real)
- Use native `page.click(...)` then `wait_for_selector(<something only the new state has>)`. `element.click()` inside `page.evaluate` and `wait_for_load_state("networkidle")` both gave false negatives with HTMX swaps.
- Clicks can start a Gridstack drag/resize, which **persists** to `layout_state`. On the live instance, check that table after any automation; one stray drag once scrambled the whole layout.
- `#dashboard-grid` scrolls internally, so `full_page=True` screenshots stop at the viewport. Scroll it with `page.evaluate("document.getElementById('dashboard-grid').scrollTop = 99999")`.
- Measure, don't guess, when layout looks wrong: `el.getBoundingClientRect()` and `getComputedStyle(el)` up the ancestor chain found the calendar overflow bug in one pass after several wrong CSS theories.
- Admin POSTs from scripts need a same-origin `Origin` header or none at all; a foreign `Origin` is refused with 403 by design.
