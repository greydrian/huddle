"""
Google Tasks API client — shopping list + per-person task list sync.

Same shape as app/google_oauth.py's Calendar functions: httpx directly
against Google's REST endpoints, no SDK. OAuth (tokens, scopes, refresh)
lives in google_oauth.py; this module is purely the Tasks API surface.
"""

from urllib.parse import quote

import httpx

TASKLISTS_ENDPOINT = "https://tasks.googleapis.com/tasks/v1/users/@me/lists"
TASKS_ENDPOINT_TEMPLATE = "https://tasks.googleapis.com/tasks/v1/lists/{tasklist_id}/tasks"
TASK_ENDPOINT_TEMPLATE = "https://tasks.googleapis.com/tasks/v1/lists/{tasklist_id}/tasks/{task_id}"


async def fetch_tasklists(access_token: str) -> list[dict]:
    """Every Google Tasks list on the account, for the Admin pickers."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(TASKLISTS_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        items = resp.json().get("items", [])
    return [{"id": item["id"], "title": item["title"]} for item in items]


async def fetch_tasks(access_token: str, tasklist_id: str) -> list[dict]:
    """Every task on a list, including completed/hidden ones — a status
    change made from the phone (ticking something off) still needs to show
    up here so the reconcile pass can see it."""
    url = TASKS_ENDPOINT_TEMPLATE.format(tasklist_id=quote(tasklist_id, safe=""))
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"showCompleted": "true", "showHidden": "true", "maxResults": 100},
        )
        resp.raise_for_status()
        return resp.json().get("items", [])


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
