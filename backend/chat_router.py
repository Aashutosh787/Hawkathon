"""
chat_router.py — Multi-bot conversational chat powered by Claude.
Each bot has a distinct system prompt tailored to Monroe career use-cases.
Chat history is persisted in SQLite and replayed on every request so the
model has full context without an in-process session object.
"""
import base64
import json
import logging
import os

import aiosqlite
from anthropic import Anthropic
from datetime import datetime, timezone
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from auth_router import get_current_user
from database import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat")

CHAT_MODEL = "claude-haiku-4-5-20251001"

# ── Report-context bot base prompts ───────────────────────────────────────────
# These are enriched at runtime with the user's actual report data.

REPORT_BOT_BASE: dict[str, str] = {
    "report-resume": (
        "You are Tether's Resume AI advisor for a ULM / BPCC student. "
        "The student has already received an AI-generated resume analysis shown below. "
        "Answer questions about their resume, skill gaps, and how to improve. "
        "Be specific — reference their actual skills and gaps.\n\n"
        "=== THEIR RESUME ANALYSIS ===\n{context}\n==========================="
    ),
    "report-jobs": (
        "You are Tether's Job Match advisor for a ULM / BPCC student. "
        "The student has already received AI-matched Monroe-area job recommendations shown below. "
        "Answer questions about these specific employers, roles, salaries, and how to apply. "
        "Reference their actual matches by name.\n\n"
        "=== THEIR JOB MATCHES ===\n{context}\n========================="
    ),
    "report-salary": (
        "You are Tether's Salary & Career Growth advisor for a ULM / BPCC student. "
        "The student has a salary analysis comparing Monroe vs Dallas shown below. "
        "Answer questions about their earning potential, cost-of-living, and career growth. "
        "Use their exact numbers.\n\n"
        "=== THEIR SALARY ANALYSIS ===\n{context}\n============================="
    ),
    "report-plan": (
        "You are Tether's Career Roadmap advisor for a ULM / BPCC student. "
        "The student has a personalised 90-day action plan shown below. "
        "Answer questions about specific steps, how to execute them, and what comes after. "
        "Be concrete — name real ULM offices, platforms, and contacts where relevant.\n\n"
        "=== THEIR 90-DAY PLAN ===\n{context}\n========================="
    ),
}

REPORT_BOT_TYPES = set(REPORT_BOT_BASE.keys())


async def _load_user_report(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT report_json FROM user_reports WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row["report_json"]) if row else None


def _build_report_system_prompt(bot_type: str, report: dict) -> str:
    """Inject the relevant report section into the system prompt."""
    template = REPORT_BOT_BASE[bot_type]
    if bot_type == "report-resume":
        ctx = (
            f"Student summary: {report.get('student_summary', '')}\n\n"
            "Skill gaps:\n" +
            "\n".join(
                f"- {g['skill']}: {g['why_it_matters_locally']} | "
                f"How to learn: {g['how_to_learn']}"
                for g in report.get("skill_gaps", [])
            )
        )
    elif bot_type == "report-jobs":
        ctx = "\n".join(
            f"{i+1}. {e['name']} — {e['role_to_target']} "
            f"(match: {e['match_score']}%)\n   {e['why_fit']}"
            for i, e in enumerate(report.get("top_employers", []))
        )
    elif bot_type == "report-salary":
        sal = report.get("salary_trajectory", {})
        ctx = (
            f"Monroe entry: ${sal.get('entry_monroe', 0):,} | "
            f"Monroe mid: ${sal.get('mid_monroe', 0):,}\n"
            f"Dallas entry: ${sal.get('entry_dallas', 0):,} | "
            f"Cost-adjusted Monroe equivalent: "
            f"${sal.get('cost_adjusted_monroe_equivalent_to_dallas', 0):,}\n"
            f"Commentary: {sal.get('commentary', '')}"
        )
    elif bot_type == "report-plan":
        ctx = "\n".join(
            f"{item['week_range']}: {item['action']}"
            for item in report.get("action_plan_90_days", [])
        )
    else:
        ctx = json.dumps(report, indent=2)
    return template.format(context=ctx)


SYSTEM_PROMPTS: dict[str, str] = {
    "resume-checker": """\
You are Tether's Resume Checker AI — an expert career counselor specializing in
resume analysis for University of Louisiana at Monroe (ULM) and BPCC students.

Your job:
- Analyze resumes and give detailed, actionable feedback
- Highlight strengths and weaknesses concisely
- Suggest formatting, content, and keyword improvements
- Tailor advice to the Monroe / Northeast Louisiana job market
- Name real local employers where relevant (IBM CIC, Lumen, Ochsner LSU Health, etc.)

Be encouraging but honest. Use clear sections and bullet points.""",

    "job-scanner": """\
You are Tether's Job Scanner AI — helping ULM and BPCC students find real jobs
in Monroe, West Monroe, and Northeast Louisiana.

Your job:
- Identify local opportunities matching the student's skills and major
- Share intel on major Monroe employers and their hiring patterns
- Discuss Northeast Louisiana industry trends
- Suggest job-search strategies specific to the local market
- Provide realistic salary ranges for the area

Be proactive. Help students see opportunities they didn't know existed locally.""",

    "roadmap-generator": """\
You are Tether's Career Roadmap Generator — you create personalized 90-day
action plans for ULM and BPCC students.

Your job:
- Build detailed, week-by-week career roadmaps
- Include specific skill-building tasks and milestones
- Recommend networking moves within the Monroe community
- Suggest certifications and local campus resources (ULM Career Services, etc.)
- Set realistic timelines with checkpoints

Be structured and specific. Name real local offices, clubs, platforms, and contacts.""",

    "image-scanner": """\
You are Tether's Image Scanner AI — you analyze career-related images uploaded
by ULM and BPCC students.

Your job:
- Read screenshots of job listings and extract key requirements
- Review LinkedIn profiles and suggest improvements
- Examine certificates, transcripts, or documents
- Provide thorough observations and career-specific recommendations

Be detail-oriented. Describe what you see, then give actionable advice.""",
}


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _get_history(user_id: str, bot_type: str, limit: int = 40) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE user_id = ? AND bot_type = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (user_id, bot_type, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def _save(user_id: str, bot_type: str, role: str, content: str) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_messages (user_id, bot_type, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, bot_type, role, content, ts),
        )
        await db.commit()
    return ts


# ── Pydantic ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    bot_type: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/send")
async def send_message(req: ChatRequest, request: Request):
    user = await get_current_user(request)
    user_id = user["id"]

    is_report_bot = req.bot_type in REPORT_BOT_TYPES
    if req.bot_type not in SYSTEM_PROMPTS and not is_report_bot:
        raise HTTPException(status_code=400, detail="Invalid bot type")

    await _save(user_id, req.bot_type, "user", req.message)

    history = await _get_history(user_id, req.bot_type)

    # Build system prompt — inject report context for report-* bots
    if is_report_bot:
        report = await _load_user_report(user_id)
        if report:
            system_prompt = _build_report_system_prompt(req.bot_type, report)
        else:
            system_prompt = "You are a helpful career advisor. Ask the student to generate their report first."
    else:
        system_prompt = SYSTEM_PROMPTS[req.bot_type]

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        resp = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=history,
        )
        reply = resp.content[0].text
    except Exception as exc:
        logger.error("Anthropic chat error: %s", exc)
        raise HTTPException(status_code=502, detail="AI service temporarily unavailable")

    ts = await _save(user_id, req.bot_type, "assistant", reply)
    return {"role": "assistant", "content": reply, "bot_type": req.bot_type, "created_at": ts}


@router.get("/history/{bot_type}")
async def get_history(bot_type: str, request: Request):
    user = await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, bot_type, created_at FROM chat_messages "
            "WHERE user_id = ? AND bot_type = ? ORDER BY created_at ASC LIMIT 100",
            (user["id"], bot_type),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.delete("/history/{bot_type}")
async def clear_history(bot_type: str, request: Request):
    user = await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM chat_messages WHERE user_id = ? AND bot_type = ?",
            (user["id"], bot_type),
        )
        await db.commit()
    return {"message": "Chat history cleared"}


@router.post("/send-image")
async def send_image(
    request: Request,
    image: UploadFile = File(...),
    message: str = Form("Please analyze this image."),
    bot_type: str = Form("image-scanner"),
):
    user = await get_current_user(request)
    user_id = user["id"]

    if bot_type not in SYSTEM_PROMPTS:
        raise HTTPException(status_code=400, detail="Invalid bot type")

    raw = await image.read()
    b64 = base64.standard_b64encode(raw).decode()
    media_type = image.content_type or "image/jpeg"

    user_content = f"[Image: {image.filename}] {message}"
    await _save(user_id, bot_type, "user", user_content)

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        resp = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPTS.get(bot_type, "You are a helpful assistant."),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": message},
                ],
            }],
        )
        reply = resp.content[0].text
    except Exception as exc:
        logger.error("Anthropic image error: %s", exc)
        raise HTTPException(status_code=502, detail="AI service temporarily unavailable")

    ts = await _save(user_id, bot_type, "assistant", reply)
    return {"role": "assistant", "content": reply, "bot_type": bot_type, "created_at": ts}
