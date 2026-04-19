"""
outreach_router.py — AI-powered recruiter outreach for Tether.

Flow:
1.  GET  /api/outreach/recruiters      — pull employers from user's saved report
2.  POST /api/outreach/find-email      — try to discover recruiter email for a company
3.  POST /api/outreach/draft           — generate personalised email draft with Claude
4.  GET  /api/outreach/drafts          — list all drafts for the user
5.  PUT  /api/outreach/drafts/{id}     — edit subject / body / email / status
6.  POST /api/outreach/send/{id}       — send one email via SMTP
7.  POST /api/outreach/send-all        — send all drafts that have an email address
8.  GET  /api/outreach/smtp-status     — check if SMTP creds are configured
"""

import asyncio
import base64
import json
import logging
import os
import re
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import aiosqlite
import httpx
from anthropic import Anthropic
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth_router import get_current_user
from database import DB_PATH

# Fernet key derived from JWT_SECRET so no extra env var is needed.
# Just needs to be a valid 32-byte URL-safe base64 key.
def _fernet() -> Fernet:
    raw = os.environ.get("JWT_SECRET", "default-secret-please-change")
    # Pad/truncate to 32 bytes and base64-encode
    key_bytes = raw.encode()[:32].ljust(32, b"\x00")
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/outreach")

_ANTHROPIC = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ── Recruiter email discovery ──────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_SKIP_PATTERNS = ("noreply", "no-reply", "example", "test@", "@w3", "sentry", "schema", "@2x", "email@email")

# Hunter.io recruiting-relevant job titles to prioritise
_RECRUITING_TITLES = (
    "recruiter", "talent", "hr", "human resource", "people", "hiring",
    "acquisition", "workforce", "staffing",
)


def _company_domain(name: str) -> str:
    """Best-guess domain from company name."""
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    return f"{slug}.com"


async def _hunter_search(domain: str, api_key: str) -> list[str]:
    """
    Call Hunter.io Domain Search API and return up to 5 emails,
    prioritising recruiting/HR contacts.
    https://hunter.io/api-keys  (free tier: 25 searches/month)
    """
    url = "https://api.hunter.io/v2/domain-search"
    params = {"domain": domain, "api_key": api_key, "limit": 20, "type": "personal"}

    async with httpx.AsyncClient(timeout=8) as client:
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
        except Exception:
            return []

    data = r.json().get("data", {})
    emails_raw: list[dict] = data.get("emails", [])
    if not emails_raw:
        return []

    # Sort: recruiting titles first, then by confidence descending
    def _score(e: dict) -> tuple[int, int]:
        position = (e.get("position") or "").lower()
        is_recruiting = any(t in position for t in _RECRUITING_TITLES)
        return (0 if is_recruiting else 1, -(e.get("confidence") or 0))

    emails_raw.sort(key=_score)
    return [e["value"] for e in emails_raw if e.get("value")][:5]


async def _scrape_website_emails(domain: str) -> list[str]:
    """Fallback: scrape the company website for any email addresses."""
    found: list[str] = []
    async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
        for path in ["/contact", "/careers", "/about", "/about-us", ""]:
            try:
                r = await client.get(
                    f"https://{domain}{path}",
                    headers={"User-Agent": "Mozilla/5.0 (compatible; TetherBot/1.0)"},
                )
                if r.status_code == 200:
                    emails = _EMAIL_RE.findall(r.text)
                    found.extend(
                        e for e in emails
                        if not any(skip in e.lower() for skip in _SKIP_PATTERNS)
                    )
            except Exception:
                continue

    seen: set[str] = set()
    clean: list[str] = []
    for e in found:
        el = e.lower()
        if el not in seen:
            seen.add(el)
            clean.append(e)
    return clean[:5]


async def _discover_emails(company_name: str) -> tuple[list[str], str]:
    """
    Try to find real recruiter emails for a company.
    Priority order:
      1. Hunter.io API (if HUNTER_API_KEY is set) — verified emails, recruiting contacts first
      2. Website scraper — extracts emails from company's own pages
      3. Pattern fallback — common hr@/careers@ guesses
    Returns (emails, source) where source is 'hunter' | 'scraped' | 'pattern'.
    """
    domain = _company_domain(company_name)

    # 1 — Hunter.io
    hunter_key = os.environ.get("HUNTER_API_KEY", "").strip()
    if hunter_key:
        emails = await _hunter_search(domain, hunter_key)
        if emails:
            return emails, "hunter"

    # 2 — Website scraper
    emails = await _scrape_website_emails(domain)
    if emails:
        return emails, "scraped"

    # 3 — Pattern guess
    patterns = [f"careers@{domain}", f"hr@{domain}"]
    return patterns, "pattern"


# ── Email draft generation ─────────────────────────────────────────────────────

async def _generate_draft(
    student_summary: str,
    major: str,
    school: str,
    employer_name: str,
    role_to_target: str,
    why_fit: str,
) -> dict:
    """Ask Claude to draft a personalised outreach email."""
    prompt = f"""You are helping a Monroe, Louisiana student reach out to a local employer.

Student profile:
- School: {school}
- Major: {major}
- Background: {student_summary}

Target employer:
- Company: {employer_name}
- Role they should pursue: {role_to_target}
- Why they're a strong fit: {why_fit}

Write a professional cold outreach email the student can send to the company's recruiter or hiring manager.

Rules:
- Subject line: specific, not generic
- Body: 3 short paragraphs — intro + value add + call to action
- Under 180 words total in the body
- Mention Monroe/Louisiana connection naturally
- End with a specific, low-pressure ask (brief call or coffee chat)
- Do NOT use the student's name — leave a [Your Name] placeholder
- Sound like a real person, not a template

Return ONLY valid JSON, nothing else:
{{"subject": "...", "body": "..."}}"""

    msg = _ANTHROPIC.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # Strip markdown fences if Claude added them
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return {
        "subject": f"Interest in opportunities at {employer_name}",
        "body": text,
    }


# ── Per-user SMTP ──────────────────────────────────────────────────────────────

async def _get_user_smtp(user_id: str) -> tuple[str, str] | None:
    """Return (smtp_user, smtp_pass) for the user, or None if not configured."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT smtp_user, smtp_pass_enc FROM users WHERE id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row["smtp_user"] or not row["smtp_pass_enc"]:
        return None
    try:
        pwd = _decrypt(row["smtp_pass_enc"])
        return row["smtp_user"], pwd
    except Exception:
        return None


def _send_smtp(from_email: str, smtp_pass: str, to_email: str, subject: str, body: str) -> None:
    """Send an email using the supplied credentials (always the user's own account)."""
    host = "smtp.gmail.com"
    port = 587
    # Detect Outlook/Hotmail
    if "outlook" in from_email or "hotmail" in from_email or "live" in from_email:
        host = "smtp.office365.com"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(from_email, smtp_pass)
        smtp.sendmail(from_email, to_email, msg.as_string())


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_user_report(user_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT report_json, major, school FROM user_reports WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No report found — generate your career report first.")
    return {
        "report": json.loads(row["report_json"]),
        "major": row["major"] or "",
        "school": row["school"] or "",
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

class SmtpSettingsRequest(BaseModel):
    smtp_user: str
    smtp_pass: str


@router.post("/smtp-settings")
async def save_smtp_settings(req: SmtpSettingsRequest, user=Depends(get_current_user)):
    """Store the user's own SMTP credentials (encrypted). Email sends come from their address."""
    enc = _encrypt(req.smtp_pass)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET smtp_user = ?, smtp_pass_enc = ? WHERE id = ?",
            (req.smtp_user, enc, user["id"]),
        )
        await db.commit()
    return {"message": "Email settings saved"}


@router.delete("/smtp-settings")
async def clear_smtp_settings(user=Depends(get_current_user)):
    """Remove stored SMTP credentials."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET smtp_user = NULL, smtp_pass_enc = NULL WHERE id = ?",
            (user["id"],),
        )
        await db.commit()
    return {"message": "Email settings cleared"}


@router.get("/smtp-status")
async def smtp_status(user=Depends(get_current_user)):
    creds = await _get_user_smtp(user["id"])
    return {
        "configured": creds is not None,
        "email": creds[0] if creds else None,
    }


@router.get("/recruiters")
async def get_recruiters(user=Depends(get_current_user)):
    """Return employers from the user's report + their saved outreach draft state."""
    data = await _get_user_report(user["id"])
    employers = data["report"].get("top_employers", [])

    # Enrich with saved draft status
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT employer_id, employer_email, email_source, subject, body, status, sent_at, error "
            "FROM outreach_drafts WHERE user_id = ?",
            (user["id"],),
        ) as cur:
            rows = await cur.fetchall()

    draft_map = {r["employer_id"]: dict(r) for r in rows}

    result = []
    for emp in employers:
        saved = draft_map.get(emp["id"], {})
        result.append({
            **emp,
            "employer_email": saved.get("employer_email") or "",
            "email_source": saved.get("email_source") or "",
            "draft_subject": saved.get("subject") or "",
            "draft_body": saved.get("body") or "",
            "status": saved.get("status") or "no_draft",
            "sent_at": saved.get("sent_at"),
            "error": saved.get("error"),
        })

    return {
        "employers": result,
        "major": data["major"],
        "school": data["school"],
        "student_summary": data["report"].get("student_summary", ""),
    }


class FindEmailRequest(BaseModel):
    employer_id: str
    employer_name: str


@router.post("/find-email")
async def find_email(req: FindEmailRequest, user=Depends(get_current_user)):
    """Attempt to discover recruiter emails for a company via web scraping."""
    emails, source = await _discover_emails(req.employer_name)

    # Persist the best email guess to the draft row
    if emails:
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO outreach_drafts
                   (id, user_id, employer_id, employer_name, employer_email, email_source,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'no_draft', ?)
                   ON CONFLICT(user_id, employer_id) DO UPDATE SET
                     employer_email = excluded.employer_email,
                     email_source   = excluded.email_source""",
                (str(uuid.uuid4()), user["id"], req.employer_id, req.employer_name,
                 emails[0], source, ts),
            )
            await db.commit()

    return {"emails": emails, "source": source, "primary": emails[0] if emails else ""}


class SetEmailRequest(BaseModel):
    employer_id: str
    employer_name: str
    email: str


@router.post("/set-email")
async def set_email(req: SetEmailRequest, user=Depends(get_current_user)):
    """Manually set the recruiter email for an employer."""
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO outreach_drafts
               (id, user_id, employer_id, employer_name, employer_email, email_source,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, 'manual', 'no_draft', ?)
               ON CONFLICT(user_id, employer_id) DO UPDATE SET
                 employer_email = excluded.employer_email,
                 email_source   = 'manual'""",
            (str(uuid.uuid4()), user["id"], req.employer_id, req.employer_name,
             req.email, ts),
        )
        await db.commit()
    return {"message": "Email saved"}


class DraftRequest(BaseModel):
    employer_id: str
    employer_name: str
    role_to_target: str
    why_fit: str
    auto_send: bool = False


@router.post("/draft")
async def create_draft(req: DraftRequest, user=Depends(get_current_user)):
    """Generate an AI email draft for a specific employer. Optionally send immediately."""
    data = await _get_user_report(user["id"])

    draft = await _generate_draft(
        student_summary=data["report"].get("student_summary", ""),
        major=data["major"],
        school=data["school"],
        employer_name=req.employer_name,
        role_to_target=req.role_to_target,
        why_fit=req.why_fit,
    )

    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO outreach_drafts
               (id, user_id, employer_id, employer_name, role_to_target,
                subject, body, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)
               ON CONFLICT(user_id, employer_id) DO UPDATE SET
                 role_to_target = excluded.role_to_target,
                 subject        = excluded.subject,
                 body           = excluded.body,
                 status         = 'draft'""",
            (str(uuid.uuid4()), user["id"], req.employer_id, req.employer_name,
             req.role_to_target, draft["subject"], draft["body"], ts),
        )
        await db.commit()

    if req.auto_send:
        creds = await _get_user_smtp(user["id"])
        if creds:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT employer_email FROM outreach_drafts WHERE user_id = ? AND employer_id = ?",
                    (user["id"], req.employer_id),
                ) as cur:
                    row = await cur.fetchone()
            if row and row["employer_email"]:
                from_email, smtp_pass = creds
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None, _send_smtp, from_email, smtp_pass,
                        row["employer_email"], draft["subject"], draft["body"],
                    )
                    sent_ts = datetime.now(timezone.utc).isoformat()
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE outreach_drafts SET status='sent', sent_at=? "
                            "WHERE user_id=? AND employer_id=?",
                            (sent_ts, user["id"], req.employer_id),
                        )
                        await db.commit()
                    return {**draft, "status": "sent", "auto_sent": True}
                except Exception as exc:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE outreach_drafts SET status='failed', error=? "
                            "WHERE user_id=? AND employer_id=?",
                            (str(exc), user["id"], req.employer_id),
                        )
                        await db.commit()
                    return {**draft, "status": "failed", "error": str(exc), "auto_sent": True}

    return {**draft, "status": "draft", "auto_sent": False}


class UpdateDraftRequest(BaseModel):
    employer_id: str
    subject: Optional[str] = None
    body: Optional[str] = None
    employer_email: Optional[str] = None


@router.put("/draft")
async def update_draft(req: UpdateDraftRequest, user=Depends(get_current_user)):
    """Edit a draft's content."""
    updates, values = [], []
    if req.subject is not None:
        updates.append("subject = ?"); values.append(req.subject)
    if req.body is not None:
        updates.append("body = ?"); values.append(req.body)
    if req.employer_email is not None:
        updates.append("employer_email = ?"); values.append(req.employer_email)
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    values.extend([user["id"], req.employer_id])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE outreach_drafts SET {', '.join(updates)} "
            "WHERE user_id = ? AND employer_id = ?",
            values,
        )
        await db.commit()
    return {"message": "Updated"}


@router.post("/send/{employer_id}")
async def send_email_for_employer(employer_id: str, user=Depends(get_current_user)):
    """Send the draft using the student's own configured email credentials."""
    creds = await _get_user_smtp(user["id"])
    if not creds:
        raise HTTPException(
            status_code=400,
            detail="No email account connected — add your email credentials in Outreach settings first.",
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM outreach_drafts WHERE user_id = ? AND employer_id = ?",
            (user["id"], employer_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No draft found for this employer")
    draft = dict(row)
    if not draft.get("employer_email"):
        raise HTTPException(status_code=400, detail="No recipient email — add one first")
    if not draft.get("subject") or not draft.get("body"):
        raise HTTPException(status_code=400, detail="Draft is empty — generate a draft first")

    from_email, smtp_pass = creds
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _send_smtp, from_email, smtp_pass,
            draft["employer_email"], draft["subject"], draft["body"],
        )
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE outreach_drafts SET status='sent', sent_at=?, error=NULL "
                "WHERE user_id=? AND employer_id=?",
                (ts, user["id"], employer_id),
            )
            await db.commit()
        return {"message": "Sent", "sent_at": ts}
    except Exception as exc:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE outreach_drafts SET status='failed', error=? "
                "WHERE user_id=? AND employer_id=?",
                (str(exc), user["id"], employer_id),
            )
            await db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/send-all")
async def send_all(user=Depends(get_current_user)):
    """Send all drafted emails using the student's own configured credentials."""
    creds = await _get_user_smtp(user["id"])
    if not creds:
        raise HTTPException(
            status_code=400,
            detail="No email account connected — add your credentials in Outreach settings first.",
        )
    from_email, smtp_pass = creds

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM outreach_drafts
               WHERE user_id = ?
                 AND status IN ('draft', 'approved')
                 AND employer_email IS NOT NULL AND employer_email != ''
                 AND subject IS NOT NULL AND body IS NOT NULL""",
            (user["id"],),
        ) as cur:
            rows = await cur.fetchall()

    results = []
    for row in rows:
        d = dict(row)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, _send_smtp, from_email, smtp_pass,
                d["employer_email"], d["subject"], d["body"],
            )
            ts = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE outreach_drafts SET status='sent', sent_at=? WHERE user_id=? AND employer_id=?",
                    (ts, user["id"], d["employer_id"]),
                )
                await db.commit()
            results.append({"employer": d["employer_name"], "status": "sent"})
        except Exception as exc:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE outreach_drafts SET status='failed', error=? WHERE user_id=? AND employer_id=?",
                    (str(exc), user["id"], d["employer_id"]),
                )
                await db.commit()
            results.append({"employer": d["employer_name"], "status": "failed", "error": str(exc)})

    return {
        "results": results,
        "sent": sum(1 for r in results if r["status"] == "sent"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    }
