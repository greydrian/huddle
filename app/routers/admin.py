"""
Admin / Parent Controls (Section 4.7): single shared PIN, exponential backoff
on failed attempts, short-lived signed session cookie. Manages family
profiles, tasks/points, rewards, and (eventually) Google account connections.
"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import get_db, get_setting, set_setting
from app.security import verify_pin, hash_pin, lockout_seconds_for, create_session_token, verify_session_token
from app.templating import templates

router = APIRouter(prefix="/admin")

SESSION_COOKIE = "admin_session"


async def require_admin(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not verify_session_token(token):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request, pin: str = Form(...)):
    async with get_db() as db:
        lockout_raw = await get_setting(db, "pin_lockout", "{}")
        lockout = json.loads(lockout_raw)
        locked_until = lockout.get("locked_until")

        if locked_until and datetime.utcnow() < datetime.fromisoformat(locked_until):
            wait_seconds = int((datetime.fromisoformat(locked_until) - datetime.utcnow()).total_seconds())
            return templates.TemplateResponse(
                request,
                "admin/login.html",
                {"error": f"Too many attempts. Try again in {wait_seconds}s."},
                status_code=429,
            )

        stored_hash = await get_setting(db, "pin_hash")
        if stored_hash and verify_pin(pin, stored_hash):
            await set_setting(db, "pin_lockout", json.dumps({}))
            await db.commit()
            response = RedirectResponse(url="/admin", status_code=303)
            response.set_cookie(
                SESSION_COOKIE, create_session_token(), httponly=True, samesite="lax"
            )
            return response

        # Failed attempt: bump the counter and set an exponential-backoff lockout
        failed_attempts = lockout.get("failed_attempts", 0) + 1
        wait = lockout_seconds_for(failed_attempts)
        locked_until_new = (datetime.utcnow() + timedelta(seconds=wait)).isoformat()
        await set_setting(
            db,
            "pin_lockout",
            json.dumps({"failed_attempts": failed_attempts, "locked_until": locked_until_new}),
        )
        await db.commit()

    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"error": f"Incorrect PIN. Try again in {wait}s." if wait else "Incorrect PIN."},
        status_code=401,
    )


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_home(request: Request):
    async with get_db() as db:
        profiles = [dict(r) for r in await (await db.execute(
            "SELECT * FROM profiles ORDER BY sort_order"
        )).fetchall()]
        tasks = [dict(r) for r in await (await db.execute(
            "SELECT tasks.*, profiles.name as profile_name FROM tasks "
            "JOIN profiles ON profiles.id = tasks.profile_id "
            "WHERE archived = 0 ORDER BY profiles.sort_order, tasks.created_at"
        )).fetchall()]
        rewards = [dict(r) for r in await (await db.execute(
            "SELECT * FROM rewards ORDER BY points_cost"
        )).fetchall()]

    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"profiles": profiles, "tasks": tasks, "rewards": rewards},
    )


# --- Family member management ---

@router.post("/profiles", dependencies=[Depends(require_admin)])
async def add_profile(name: str = Form(...), colour_hex: str = Form(...)):
    async with get_db() as db:
        cursor = await db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM profiles")
        (next_order,) = await cursor.fetchone()
        await db.execute(
            "INSERT INTO profiles (name, colour_hex, sort_order) VALUES (?, ?, ?)",
            (name.strip(), colour_hex, next_order),
        )
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/profiles/{profile_id}/delete", dependencies=[Depends(require_admin)])
async def delete_profile(profile_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- Task management ---

@router.post("/tasks", dependencies=[Depends(require_admin)])
async def add_task(
    profile_id: int = Form(...),
    title: str = Form(...),
    points: int = Form(0),
    is_recurring: bool = Form(False),
    recurrence_rule: str = Form(""),
):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO tasks (profile_id, title, points, is_recurring, recurrence_rule)
               VALUES (?, ?, ?, ?, ?)""",
            (profile_id, title.strip(), points, int(is_recurring), recurrence_rule.strip() or None),
        )
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/tasks/{task_id}/delete", dependencies=[Depends(require_admin)])
async def delete_task(task_id: int):
    async with get_db() as db:
        await db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (task_id,))
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- Reward management ---

@router.post("/rewards", dependencies=[Depends(require_admin)])
async def add_reward(title: str = Form(...), points_cost: int = Form(...)):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO rewards (title, points_cost) VALUES (?, ?)",
            (title.strip(), points_cost),
        )
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/rewards/{reward_id}/delete", dependencies=[Depends(require_admin)])
async def delete_reward(reward_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM rewards WHERE id = ?", (reward_id,))
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- PIN management ---

@router.post("/change-pin", dependencies=[Depends(require_admin)])
async def change_pin(new_pin: str = Form(...)):
    async with get_db() as db:
        await set_setting(db, "pin_hash", hash_pin(new_pin))
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)
