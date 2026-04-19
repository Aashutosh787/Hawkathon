"""
contacts_router.py — Deterministic HR/recruiter contact generation and saved contacts management.

Contacts are seeded by the company domain using hashlib so the same company
always returns the same names, emails, and titles — no external API needed.
"""
import hashlib
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime, timezone

import aiosqlite
from anthropic import Anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_router import get_current_user
from database import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/contacts")

# ── Contact generation data ────────────────────────────────────────────────────

_FIRST_NAMES = [
    "Amanda", "Ashley", "Brittany", "Caitlin", "Chelsea", "Christina", "Claire",
    "Danielle", "Emily", "Hannah", "Jennifer", "Jessica", "Jordan", "Karen",
    "Kayla", "Kimberly", "Lauren", "Lindsay", "Megan", "Melissa", "Michelle",
    "Morgan", "Nicole", "Rachel", "Rebecca", "Samantha", "Sarah", "Stephanie",
    "Taylor", "Tiffany", "Whitney",
    "Andrew", "Brandon", "Brian", "Charles", "Christopher", "Daniel", "David",
    "Derek", "Eric", "Ethan", "James", "Jason", "Jeff", "John", "Jonathan",
    "Kevin", "Kyle", "Mark", "Matthew", "Michael", "Nathan", "Patrick",
    "Robert", "Ryan", "Scott", "Sean", "Steven", "Thomas", "Timothy", "Tyler",
    "William", "Zachary",
]

_LAST_NAMES = [
    "Adams", "Allen", "Anderson", "Bailey", "Baker", "Barnes", "Bell",
    "Bennett", "Brooks", "Brown", "Butler", "Campbell", "Carter", "Clark",
    "Collins", "Cook", "Cooper", "Cox", "Davis", "Edwards", "Evans",
    "Fisher", "Foster", "Garcia", "Gonzalez", "Gray", "Green", "Griffin",
    "Hall", "Harris", "Henderson", "Hill", "Howard", "Hughes", "Jackson",
    "James", "Johnson", "Jones", "Kelly", "Kim", "King", "Lee", "Lewis",
    "Long", "Lopez", "Martin", "Martinez", "Miller", "Mitchell", "Moore",
    "Morgan", "Morris", "Murphy", "Nelson", "Parker", "Patel", "Patterson",
    "Perez", "Phillips", "Price", "Reed", "Richardson", "Rivera", "Roberts",
    "Robinson", "Rodriguez", "Ross", "Russell", "Sanchez", "Scott", "Simmons",
    "Smith", "Stewart", "Sullivan", "Taylor", "Thomas", "Thompson", "Torres",
    "Turner", "Walker", "Ward", "Watson", "White", "Williams", "Wilson",
    "Wood", "Wright", "Young",
]

# (title, confidence_score)
_TITLES = [
    ("HR Manager",                     82),
    ("Senior Recruiter",               88),
    ("Talent Acquisition Specialist",  76),
    ("Recruiting Manager",             84),
    ("HR Director",                    72),
    ("Human Resources Coordinator",    66),
    ("Talent Acquisition Manager",     80),
    ("HR Generalist",                  62),
    ("Recruiting Coordinator",         67),
    ("People Operations Manager",      74),
    ("Staffing Specialist",            70),
    ("HR Business Partner",            78),
    ("Talent Acquisition Lead",        79),
    ("Senior HR Specialist",           71),
    ("Campus Recruiter",               73),
    ("Corporate Recruiter",            85),
    ("Director of Talent Acquisition", 69),
    ("VP of Human Resources",          65),
    ("Workforce Planning Manager",     68),
    ("HR Operations Specialist",       64),
]

_EMAIL_FORMATS = [
    lambda f, l: f"{f.lower()}.{l.lower()}",
    lambda f, l: f"{f[0].lower()}{l.lower()}",
    lambda f, l: f"{f.lower()}{l[0].lower()}",
    lambda f, l: f"{f.lower()}_{l.lower()}",
    lambda f, l: f"{f.lower()}{l.lower()}",
    lambda f, l: f"{f.lower()}.{l.lower()[:4]}",
    lambda f, l: f"{l.lower()}.{f.lower()}",
]

# Generic role-based aliases always appended after named contacts
_GENERIC_ALIASES: list[tuple[str, str, str]] = [
    ("hr",                "HR Team",         "Human Resources"),
    ("recruiting",        "Recruiting Team", "Recruiting"),
    ("careers",           "Careers Team",    "Careers"),
    ("talent",            "Talent Team",     "Talent Acquisition"),
    ("hiring",            "Hiring Team",     "Hiring"),
    ("jobs",              "Jobs Inbox",      "Jobs"),
    ("staffing",          "Staffing Team",   "Staffing"),
    ("people",            "People Team",     "People Operations"),
    ("talentacquisition", "TA Team",         "Talent Acquisition"),
    ("hrteam",            "HR Team",         "Human Resources"),
    ("apply",             "Applications",    "Applications"),
    ("employment",        "Employment",      "Employment"),
    ("humanresources",    "HR Team",         "Human Resources"),
    ("workforce",         "Workforce Team",  "Workforce"),
    ("info",              "Info",            "General Inquiry"),
    ("contact",           "Contact",         "General Inquiry"),
    ("admin",             "Admin",           "Administration"),
    ("office",            "Office",          "General Inquiry"),
    ("inquiries",         "Inquiries",       "General Inquiry"),
    ("connect",           "Connect",         "General Inquiry"),
]


def _company_domain(name: str) -> str:
    s = name.strip()
    s = re.split(r"\s*[-–|]\s*", s)[0].strip()
    s = re.sub(
        r",?\s*\b(inc|llc|ltd|corp|co|company|group|holdings|international|"
        r"services|solutions|technologies|tech|systems|associates|partners|enterprises)\b\.?",
        "", s, flags=re.IGNORECASE,
    )
    slug = re.sub(r"[^a-z0-9]", "", s.lower()).strip()
    if not slug:
        slug = re.sub(r"[^a-z0-9]", "", name.lower())
    return f"{slug}.com"


def _generate_contacts(domain: str, named_count: int = 12) -> list[dict]:
    """
    Deterministically generate realistic HR contacts seeded by domain.
    The same domain always produces the same people. Generic alias emails
    are appended after the named contacts.
    """
    seed = int(hashlib.md5(domain.encode()).hexdigest(), 16) % (2 ** 32)
    rng = random.Random(seed)

    first_pool = list(_FIRST_NAMES)
    last_pool  = list(_LAST_NAMES)
    title_pool = list(_TITLES)
    rng.shuffle(first_pool)
    rng.shuffle(last_pool)
    rng.shuffle(title_pool)

    contacts: list[dict] = []
    used_emails: set[str] = set()

    for i in range(named_count):
        first = first_pool[i % len(first_pool)]
        last  = last_pool[i % len(last_pool)]
        title, confidence = title_pool[i % len(title_pool)]
        fmt   = rng.choice(_EMAIL_FORMATS)
        email = f"{fmt(first, last)}@{domain}"
        if email in used_emails:
            email = f"{first.lower()}.{last.lower()}{i}@{domain}"
        used_emails.add(email)
        contacts.append({
            "email":        email,
            "first_name":   first,
            "last_name":    last,
            "position":     title,
            "confidence":   confidence,
            "linkedin_url": "",
            "type":         "personal",
            "source":       "generated",
        })

    for alias, display, position in _GENERIC_ALIASES:
        generic_email = f"{alias}@{domain}"
        if generic_email not in used_emails:
            used_emails.add(generic_email)
            contacts.append({
                "email":        generic_email,
                "first_name":   display,
                "last_name":    "",
                "position":     position,
                "confidence":   0,
                "linkedin_url": "",
                "type":         "generic",
                "source":       "generic",
            })

    return contacts


# ── Find contacts ──────────────────────────────────────────────────────────────

class FindContactsRequest(BaseModel):
    company_name: str
    job_title: str = ""


@router.post("/find")
async def find_contacts(req: FindContactsRequest, user=Depends(get_current_user)):
    domain = _company_domain(req.company_name)

    if not domain or len(domain) < 4 or domain == ".com":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot derive a valid domain from company name: '{req.company_name}'",
        )

    logger.info("Generating contacts for domain=%s", domain)
    contacts = _generate_contacts(domain)

    return {
        "contacts":     contacts,
        "domain":       domain,
        "company":      req.company_name,
        "organization": req.company_name,
    }


# ── Draft email ────────────────────────────────────────────────────────────────

class DraftEmailRequest(BaseModel):
    # Recipient info
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    company_name: str
    job_title: str
    job_description: str = ""
    # Sender (student) info — supplied by the Automator
    sender_first_name: str = ""
    sender_last_name: str = ""
    resume_text: str = ""      # raw text extracted from uploaded PDF


@router.post("/draft-email")
async def draft_email(req: DraftEmailRequest, user=Depends(get_current_user)):
    """Draft a personalized cold outreach email using Claude and the student's profile."""
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
        major           = row["major"]  or ""
        school          = row["school"] or ""
        year            = row["year"]   or ""
        student_summary = report.get("student_summary", "")

    # Resume text from the automator overrides the stored summary when present
    background = (req.resume_text[:800].strip() if req.resume_text
                  else student_summary or "A motivated student seeking local opportunities")

    greeting_name   = req.first_name or "there"
    recipient_title = req.position or "Recruiter"
    sender_name     = " ".join(filter(None, [req.sender_first_name, req.sender_last_name])) or "[Your Name]"

    prompt = f"""You are helping a Monroe, Louisiana student write a personalized cold outreach email to a recruiter.

Recipient:
- First name: {greeting_name}
- Title: {recipient_title}
- Company: {req.company_name}

Target Job:
- Role: {req.job_title}
- Description excerpt: {req.job_description[:400] if req.job_description else "Not provided"}

Student Profile:
- Name: {sender_name}
- School: {school or "University of Louisiana Monroe"}
- Major: {major or "Not specified"}
- Year: {year or "Not specified"}
- Background / Resume: {background}

Write a professional cold email. Rules:
- Open with "Hi {greeting_name},"
- Subject: specific to the role and company (not generic)
- Body: 3 short paragraphs — who you are, why this company/role, call to action
- Under 150 words in the body
- Sign off with the student's actual name: {sender_name}
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