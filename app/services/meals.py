"""Meal plan data (Section 4.5): a simple text plan per day, no recipes."""

from datetime import date, timedelta

from app.database import family_today


async def get_week_meal_plan(db, start: date | None = None):
    """Return the next 7 days as a list of {date, weekday, is_today, description}."""
    today = await family_today(db)
    start = start or today
    days = [start + timedelta(days=i) for i in range(7)]
    rows = await (
        await db.execute(
            "SELECT * FROM meal_plans WHERE date IN ({})".format(",".join("?" for _ in days)),
            [d.isoformat() for d in days],
        )
    ).fetchall()
    plan_by_date = {row["date"]: row["meal_description"] for row in rows}

    return [
        {
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "is_today": d == today,
            "description": plan_by_date.get(d.isoformat(), ""),
        }
        for d in days
    ]


async def set_meal(db, meal_date: date, description: str) -> None:
    await db.execute(
        """INSERT INTO meal_plans (date, meal_description) VALUES (?, ?)
           ON CONFLICT(date) DO UPDATE SET meal_description = excluded.meal_description""",
        (meal_date.isoformat(), description.strip()),
    )
    await db.commit()
