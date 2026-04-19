"""
report_router.py — Save/retrieve the structured career report for a user.
One report per user (upsert); the report is used to enrich chat context.
"""
import json
import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth_router import get_current_user
from database import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/report")


class SaveReportRequest(BaseModel):
    report: dict
    major: str = ""
    year: str = ""
    school: str = ""


@router.post("")
async def save_report(req: SaveReportRequest, request: Request):
    user = await get_current_user(request)
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO user_reports (user_id, report_json, major, year, school, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 report_json = excluded.report_json,
                 major       = excluded.major,
                 year        = excluded.year,
                 school      = excluded.school,
                 created_at  = excluded.created_at""",
            (user["id"], json.dumps(req.report), req.major, req.year, req.school, ts),
        )
        await db.commit()
    return {"message": "Report saved"}


@router.get("")
async def get_report(request: Request):
    user = await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT report_json, major, year, school, created_at "
            "FROM user_reports WHERE user_id = ?",
            (user["id"],),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No report found")
    return {
        "report": json.loads(row["report_json"]),
        "major": row["major"],
        "year": row["year"],
        "school": row["school"],
        "created_at": row["created_at"],
    }


@router.delete("")
async def delete_report(request: Request):
    user = await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_reports WHERE user_id = ?", (user["id"],))
        await db.commit()
    return {"message": "Report deleted"}
