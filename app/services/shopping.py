"""
The single shared shopping list (Section 4.4). Every change is written to
SQLite straight away and queued for app/task_sync.py, which syncs the list
both ways with the Google Tasks list linked in Admin.
"""

from app.task_sync import queue_sync


async def get_shopping_items(db) -> list[dict]:
    rows = await (await db.execute("SELECT * FROM shopping_items ORDER BY is_checked, created_at")).fetchall()
    return [dict(row) for row in rows]


async def add_item(db, title: str) -> None:
    """Add an item (blank titles are ignored)."""
    title = title.strip()
    if title:
        cursor = await db.execute("INSERT INTO shopping_items (title) VALUES (?)", (title,))
        await queue_sync(db, "shopping", {"item_id": cursor.lastrowid})
        await db.commit()


async def toggle_item(db, item_id: int) -> bool:
    """Check or uncheck an item. False if there's no such item."""
    cursor = await db.execute("SELECT * FROM shopping_items WHERE id = ?", (item_id,))
    item = await cursor.fetchone()
    if item is None:
        return False

    new_state = 0 if item["is_checked"] else 1
    await db.execute(
        "UPDATE shopping_items SET is_checked = ?, updated_at = datetime('now') WHERE id = ?",
        (new_state, item_id),
    )
    await queue_sync(db, "shopping", {"item_id": item_id, "is_checked": bool(new_state)})
    await db.commit()
    return True


async def delete_item(db, item_id: int) -> None:
    # Capture google_task_id before deleting — the row (and this
    # column) won't exist anymore by the time the sync worker drains
    # this queue entry and needs to know what to delete on Google's side.
    cursor = await db.execute("SELECT google_task_id FROM shopping_items WHERE id = ?", (item_id,))
    existing = await cursor.fetchone()
    google_task_id = existing["google_task_id"] if existing else None

    await db.execute("DELETE FROM shopping_items WHERE id = ?", (item_id,))
    await queue_sync(db, "shopping", {"action": "delete", "item_id": item_id, "google_task_id": google_task_id})
    await db.commit()
