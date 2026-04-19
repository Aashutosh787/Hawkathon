"""
Live job listings router — pulls real Monroe-area jobs via python-jobspy.
Results are fetched synchronously in a thread-pool executor to avoid blocking
the FastAPI event loop.
"""

import asyncio
import json
import logging
import os
import re
from time import time
from typing import Any

import aiosqlite
from anthropic import Anthropic
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from auth_router import get_current_user
from database import DB_PATH

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Simple in-process TTL cache (major → (timestamp, jobs))
# Avoids hammering job boards on every tab switch.
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60 * 30  # 30 minutes


def _cached_get(key: str) -> list[dict] | None:
    entry = _cache.get(key)
    if entry and (time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, jobs: list[dict]) -> None:
    _cache[key] = (time(), jobs)


# ---------------------------------------------------------------------------
# JobSpy scraper (runs in thread pool — scrape_jobs is synchronous)
# ---------------------------------------------------------------------------

# Locations to try in order — Monroe first, then broader Louisiana
_LOCATIONS = [
    "Monroe, Louisiana",
    "West Monroe, Louisiana",
    "Shreveport, Louisiana",
    "Louisiana",
]


def _run_scrape(search_term: str, location: str, results_wanted: int) -> Any:
    """Single scrape attempt — returns a DataFrame or None."""
    from jobspy import scrape_jobs  # local import to avoid slow startup

    return scrape_jobs(
        site_name=["indeed", "linkedin", "zip_recruiter"],
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=720,          # 30 days — Monroe is a small market
        country_indeed="USA",
        verbose=0,
    )


def _scrape(search_term: str) -> list[dict]:
    """
    Synchronous scrape — called via run_in_executor.
    Tries Monroe first; falls back to broader Louisiana if no results.
    """
    query = search_term.strip() or "jobs"

    for location in _LOCATIONS:
        try:
            logger.info("JobSpy: searching '%s' in '%s'", query, location)
            jobs_df = _run_scrape(query, location, results_wanted=25)

            if jobs_df is not None and not jobs_df.empty:
                out = _df_to_list(jobs_df, max_results=20)
                logger.info(
                    "JobSpy found %d jobs for '%s' in '%s'", len(out), query, location
                )
                return out

            logger.info(
                "JobSpy: no results for '%s' in '%s' — trying next location",
                query,
                location,
            )

        except Exception as exc:
            logger.warning(
                "JobSpy scrape failed for '%s' in '%s': %s", query, location, exc
            )
            continue

    logger.warning("JobSpy: exhausted all locations for '%s'", query)
    return []


def _df_to_list(jobs_df: Any, max_results: int = 20) -> list[dict]:
    out: list[dict] = []
    for _, row in jobs_df.head(max_results).iterrows():
        out.append({
            "id":          str(row.get("id", "")),
            "title":       _safe_str(row.get("title")),
            "company":     _safe_str(row.get("company")),
            "location":    _safe_str(row.get("location")),
            "date_posted": _safe_str(row.get("date_posted")),
            "salary":      _fmt_salary(row),
            "job_url":     _safe_str(row.get("job_url")),
            "site":        _safe_str(row.get("site")),
            "description": (_safe_str(row.get("description"))[:350]
                            if row.get("description") else ""),
        })
    return out


def _safe_str(val: Any) -> str:
    if val is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(val):
            return ""
    except Exception:
        pass
    return str(val).strip()


def _fmt_salary(row: Any) -> str:
    try:
        import pandas as pd
        lo = row.get("min_amount")
        hi = row.get("max_amount")
        interval = _safe_str(row.get("interval"))
        suffix = f"/{interval}" if interval and interval not in ("", "nan") else ""
        if pd.notna(lo) and pd.notna(hi) and lo and hi:
            return f"${int(lo):,} – ${int(hi):,}{suffix}"
        if pd.notna(lo) and lo:
            return f"${int(lo):,}+{suffix}"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/api/jobs/live")
async def get_live_jobs(
    major: str = Query(default="general", max_length=100),
    force: bool = Query(default=False),
    _user=Depends(get_current_user),
):
    """
    Return live Monroe-area job listings scraped from Indeed, LinkedIn,
    and ZipRecruiter for the given major / search term.
    Tries Monroe first; falls back to broader Louisiana locations.
    Results are cached per major for 30 minutes.
    Pass force=true to bypass the cache and re-scrape immediately.
    """
    key = major.strip().lower()[:80]

    if not force:
        cached = _cached_get(key)
        if cached is not None:
            return {"jobs": cached, "count": len(cached), "from_cache": True}

    loop = asyncio.get_event_loop()
    jobs = await loop.run_in_executor(None, _scrape, major)

    # Only cache non-empty results — empty sets get re-tried next request
    if jobs:
        _cache_set(key, jobs)
    return {"jobs": jobs, "count": len(jobs), "from_cache": False}


# ---------------------------------------------------------------------------
# Job-specific 90-day action plan
# ---------------------------------------------------------------------------

class JobPlanRequest(BaseModel):
    job_title: str
    company: str
    description: str = ""
    location: str = ""
    salary: str = ""


@router.post("/api/jobs/action-plan")
async def generate_job_action_plan(req: JobPlanRequest, user=Depends(get_current_user)):
    """
    Generate a Claude-powered 90-day action plan tailored to a specific job
    listing, enriched with context from the student's saved career report.
    """
    # Pull student context from their report (best-effort — plan still works without it)
    major = school = year = student_summary = ""
    skill_gaps: list[dict] = []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT report_json, major, year, school FROM user_reports WHERE user_id = ?",
            (user["id"],),
        ) as cur:
            row = await cur.fetchone()

    if row:
        report = json.loads(row["report_json"])
        major   = row["major"]  or ""
        school  = row["school"] or ""
        year    = row["year"]   or ""
        student_summary = report.get("student_summary", "")
        skill_gaps      = report.get("skill_gaps", [])

    skill_gap_text = (
        "\n".join(f"- {g['skill']}: {g.get('how_to_learn', '')}" for g in skill_gaps)
        if skill_gaps else "None identified"
    )

    prompt = f"""You are a career advisor helping a Monroe, Louisiana student land a specific job.

Target Job:
- Title: {req.job_title}
- Company: {req.company}
- Location: {req.location or "Monroe, LA area"}
- Salary: {req.salary or "Not listed"}
- Description excerpt: {req.description[:600] if req.description else "Not provided"}

Student Profile:
- School: {school or "Monroe university"}
- Major: {major or "Not specified"}
- Year: {year or "Not specified"}
- Background: {student_summary or "Not available"}
- Skill gaps to address: {skill_gap_text}

Write a specific 90-day action plan to maximise this student's chances of getting THIS exact job.

Requirements:
- Exactly 5 items spanning the full 90 days
- Each action must reference the company or role by name where natural
- Cover: company research → application prep → skill-building → networking → follow-up
- Keep each action under 45 words — concrete and actionable, not generic advice
- Mention real resources: LinkedIn, ULM Career Services, Handshake, specific certifications

Return ONLY valid JSON, nothing else:
{{"plan": [{{"week_range": "Weeks 1-2", "action": "..."}}]}}"""

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return {"plan": []}


# ---------------------------------------------------------------------------
# Save / retrieve / delete job-specific action plans
# ---------------------------------------------------------------------------

class SavePlanRequest(BaseModel):
    job_title: str
    company: str
    location: str = ""
    salary: str = ""
    plan: list[dict]  # list of {week_range, action}


@router.post("/api/jobs/save-plan")
async def save_job_plan(req: SavePlanRequest, user=Depends(get_current_user)):
    """Persist a job-specific 90-day plan to the database."""
    import uuid
    from datetime import datetime, timezone
    plan_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO saved_job_plans
               (id, user_id, job_title, company, location, salary, plan_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, user["id"], req.job_title, req.company,
             req.location, req.salary, json.dumps(req.plan), ts),
        )
        await db.commit()
    return {"id": plan_id, "created_at": ts}


@router.get("/api/jobs/saved-plans")
async def get_saved_plans(user=Depends(get_current_user)):
    """Return all saved job plans for the current user, newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM saved_job_plans WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ) as cur:
            rows = await cur.fetchall()
    return {
        "plans": [
            {
                "id":         r["id"],
                "job_title":  r["job_title"],
                "company":    r["company"],
                "location":   r["location"] or "",
                "salary":     r["salary"] or "",
                "plan":       json.loads(r["plan_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.delete("/api/jobs/saved-plans/{plan_id}")
async def delete_saved_plan(plan_id: str, user=Depends(get_current_user)):
    """Delete a saved plan (only the owner can delete)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM saved_job_plans WHERE id = ? AND user_id = ?",
            (plan_id, user["id"]),
        )
        await db.commit()
    return {"message": "Deleted"}
