"""
Google Tasks API client — shopping list + per-person task list sync.

Same shape as app/google_oauth.py's Calendar functions: httpx directly
against Google's REST endpoints, no SDK. OAuth (tokens, scopes, refresh)
lives in google_oauth.py; this module is purely the Tasks API surface.
"""

from urllib.parse import quote

import httpx

from app.google_oauth import get_all_pages

TASKLISTS_ENDPOINT = "https://tasks.googleapis.com/tasks/v1/users/@me/lists"
TASKS_ENDPOINT_TEMPLATE = "https://tasks.googleapis.com/tasks/v1/lists/{tasklist_id}/tasks"
TASK_ENDPOINT_TEMPLATE = "https://tasks.googleapis.com/tasks/v1/lists/{tasklist_id}/tasks/{task_id}"


async def fetch_tasklists(access_token: str) -> list[dict]:
    """Every Google Tasks list on the account, for the Admin pickers."""
    items = await get_all_pages(TASKLISTS_ENDPOINT, access_token, {"maxResults": 100})
    return [{"id": item["id"], "title": item["title"]} for item in items]


async def fetch_tasks(access_token: str, tasklist_id: str) -> list[dict]:
    """Every task on a list (all pages), including completed/hidden ones —
    a status change made from the phone still needs to show up here, and a
    task missing from this result is treated as deleted on Google's side."""
    url = TASKS_ENDPOINT_TEMPLATE.format(tasklist_id=quote(tasklist_id, safe=""))
    return await get_all_pages(
        url, access_token, {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
    )


async def insert_task(access_token: str, tasklist_id: str, title: str, completed: bool = False) -> dict:
    url = TASKS_ENDPOINT_TEMPLATE.format(tasklist_id=quote(tasklist_id, safe=""))
    body = {"title": title, "status": "completed" if completed else "needsAction"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=body)
        resp.raise_for_status()
        return resp.json()


async def update_task(
    access_token: str, tasklist_id: str, task_id: str, *, title: str | None = None, completed: bool | None = None
) -> dict:
    url = TASK_ENDPOINT_TEMPLATE.format(tasklist_id=quote(tasklist_id, safe=""), task_id=quote(task_id, safe=""))
    body = {}
    if title is not None:
        body["title"] = title
    if completed is not None:
        body["status"] = "completed" if completed else "needsAction"
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, headers={"Authorization": f"Bearer {access_token}"}, json=body)
        resp.raise_for_status()
        return resp.json()


async def delete_task(access_token: str, tasklist_id: str, task_id: str):
    url = TASK_ENDPOINT_TEMPLATE.format(tasklist_id=quote(tasklist_id, safe=""), task_id=quote(task_id, safe=""))
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers={"Authorization": f"Bearer {access_token}"})
        # Already gone (404) is fine — that's the end state we wanted anyway.
        if resp.status_code not in (204, 404):
            resp.raise_for_status()
