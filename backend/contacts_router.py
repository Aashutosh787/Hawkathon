"""
contacts_router.py — Find HR/recruiter contacts via Hunter.io and manage saved contacts.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import aiosqlite
import httpx
from anthropic import Anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_router import get_current_user
from database import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/contacts")

_RECRUITING_TITLES = (
    "recruiter", "talent", "hr", "human resource", "people", "hiring",
    "acquisition", "workforce", "staffing",
)


def _company_domain(name: str) -> str:
    """Best-effort domain derivation — strips legal suffixes then slugifies."""
    s = name.strip()
    # Strip trailing location/branch info like "Walmart - Monroe, LA"
    s = re.split(r"\s*[-–|]\s*", s)[0].strip()
    # Remove common corporate/legal suffixes
    s = re.sub(
        r",?\s*\b(inc|llc|ltd|corp|co|company|group|holdings|international|"
        r"services|solutions|technologies|tech|systems|associates|partners|enterprises)\b\.?",
        "", s, flags=re.IGNORECASE,
    )
    slug = re.sub(r"[^a-z0-9]", "", s.lower()).strip()
    # Fall back to raw slug of original name if cleaning left nothing
    if not slug:
        slug = re.sub(r"[^a-z0-9]", "", name.lower())
    return f"{slug}.com"


# ── Find contacts ──────────────────────────────────────────────────────────────

class FindContactsRequest(BaseModel):
    company_name: str
    job_title: str = ""


@router.post("/find")
async def find_contacts(req: FindContactsRequest, user=Depends(get_current_user)):
    """Search Hunter.io domain-search for HR/recruiter contacts at a company."""
    domain = _company_domain(req.company_name)
    hunter_key = os.environ.get("HUNTER_API_KEY", "").strip()

    if not hunter_key:
        raise HTTPException(status_code=400, detail="Hunter.io API key not configured. Add HUNTER_API_KEY to .env")

    if not domain or len(domain) < 4 or domain == ".com":
        raise HTTPException(status_code=400, detail=f"Cannot derive a valid domain from company name: '{req.company_name}'")

    logger.info("Hunter.io domain-search: domain=%s", domain)
    async with httpx.AsyncClient(timeout=12) as client:
        try:
            r = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": hunter_key, "limit": 20},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Hunter.io request failed: {exc}")

    if r.status_code != 200:
        body = r.text[:600]
        logger.error("Hunter.io %d for domain '%s': %s", r.status_code, domain, body)
        raise HTTPException(
            status_code=502,
            detail=f"Hunter.io error {r.status_code} (domain={domain}): {body}",
        )

    data = r.json().get("data", {})
    emails_raw: list[dict] = data.get("emails", [])

    def _score(e: dict) -> tuple[int, int]:
        pos = (e.get("position") or "").lower()
        is_hr = any(t in pos for t in _RECRUITING_TITLES)
        return (0 if is_hr else 1, -(e.get("confidence") or 0))

    emails_raw.sort(key=_score)

    contacts = []
    for e in emails_raw[:15]:
        if not e.get("value"):
            continue
        contacts.append({
            "email":        e.get("value", ""),
            "first_name":   e.get("first_name") or "",
            "last_name":    e.get("last_name") or "",
            "position":     e.get("position") or "",
            "confidence":   e.get("confidence") or 0,
            "linkedin_url": e.get("linkedin") or "",   # Hunter.io field is "linkedin" not "linkedin_url"
            "type":         e.get("type") or "",
        })

    return {
        "contacts": contacts,
        "domain": domain,
        "company": req.company_name,
        "organization": data.get("organization", ""),
    }


# ── Draft email ────────────────────────────────────────────────────────────────

class DraftEmailRequest(BaseModel):
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    company_name: str
    job_title: str
    job_description: str = ""


@router.post("/draft-email")
async def draft_email(req: DraftEmailRequest, user=Depends(get_current_user)):
    """Draft a personalized cold outreach email using Claude and the student's report context."""
    major = school = year = student_summary = ""

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

    greeting_name = req.first_name or "there"
    recipient_title = req.position or "Recruiter"

    prompt = f"""You are helping a Monroe, Louisiana student write a personalized cold outreach email to a recruiter.

Recipient:
- First name: {greeting_name}
- Title: {recipient_title}
- Company: {req.company_name}

Target Job:
- Role: {req.job_title}
- Description excerpt: {req.job_description[:400] if req.job_description else "Not provided"}

Student Profile:
- School: {school or "University of Louisiana Monroe"}
- Major: {major or "Not specified"}
- Year: {year or "Not specified"}
- Background: {student_summary or "A motivated student seeking local opportunities"}

Write a professional cold email. Rules:
- Open with "Hi {greeting_name},"
- Subject: specific to the role and company (not generic)
- Body: 3 short paragraphs — who you are, why this company/role, call to action
- Under 150 words in the body
- End body with "[Your Name]" placeholder
- Sound genuine and direct, not template-like

Return ONLY valid JSON:
{{"subject": "...", "body": "..."}}"""

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return {"subject": f"Interest in {req.job_title} at {req.company_name}", "body": text}


# ── Save / list / delete contacts ──────────────────────────────────────────────

class SaveContactRequest(BaseModel):
    company: str
    company_domain: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str
    position: str = ""
    confidence: int = 0
    linkedin_url: str = ""
    job_title: str = ""
    job_url: str = ""
    draft_subject: str = ""
    draft_body: str = ""


@router.post("/save")
async def save_contact(req: SaveContactRequest, user=Depends(get_current_user)):
    contact_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO saved_contacts
               (id, user_id, company, company_domain, first_name, last_name,
                email, position, confidence, linkedin_url, job_title, job_url,
                draft_subject, draft_body, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, user["id"], req.company, req.company_domain,
             req.first_name, req.last_name, req.email, req.position,
             req.confidence, req.linkedin_url, req.job_title, req.job_url,
             req.draft_subject, req.draft_body, ts),
        )
        await db.commit()
    return {"id": contact_id, "created_at": ts}


@router.get("/saved")
async def get_saved_contacts(user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM saved_contacts WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ) as cur:
            rows = await cur.fetchall()
    return {
        "contacts": [
            {
                "id":             r["id"],
                "company":        r["company"],
                "company_domain": r["company_domain"] or "",
                "first_name":     r["first_name"] or "",
                "last_name":      r["last_name"] or "",
                "email":          r["email"],
                "position":       r["position"] or "",
                "confidence":     r["confidence"] or 0,
                "linkedin_url":   r["linkedin_url"] or "",
                "job_title":      r["job_title"] or "",
                "job_url":        r["job_url"] or "",
                "draft_subject":  r["draft_subject"] or "",
                "draft_body":     r["draft_body"] or "",
                "created_at":     r["created_at"],
            }
            for r in rows
        ]
    }


@router.delete("/saved/{contact_id}")
async def delete_contact(contact_id: str, user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM saved_contacts WHERE id = ? AND user_id = ?",
            (contact_id, user["id"]),
        )
        await db.commit()
    return {"message": "Deleted"}
