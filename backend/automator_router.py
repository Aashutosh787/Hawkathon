"""
automator_router.py — One-shot email sender for the Job Automator.

The frontend automation loop calls POST /api/automator/send-email once per
contact after it has already drafted the subject/body via Claude. This keeps
the heavy orchestration in the frontend (where progress can be streamed to the
user) while the backend handles the SMTP credential lookup and actual send.
"""
import base64
import io
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosqlite
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from pypdf import PdfReader

from auth_router import get_current_user
from database import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/automator")


def _fernet() -> Fernet:
    raw = os.environ.get("JWT_SECRET", "default-secret-please-change")
    key_bytes = raw.encode()[:32].ljust(32, b"\x00")
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


@router.post("/parse-resume")
async def parse_resume(pdf: UploadFile = File(...), _user=Depends(get_current_user)):
    """Extract plain text from an uploaded PDF resume."""
    data = await pdf.read()
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}")
    if not text:
        raise HTTPException(status_code=400, detail="PDF appears to be empty or image-only.")
    return {"text": text}


class SendEmailRequest(BaseModel):
    to_email: str
    subject: str
    body: str


@router.post("/send-email")
async def automator_send_email(req: SendEmailRequest, user=Depends(get_current_user)):
    """Send a single email using the user's stored SMTP credentials."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT smtp_user, smtp_pass_enc FROM users WHERE id = ?",
            (user["id"],),
        ) as cur:
            row = await cur.fetchone()

    if not row or not row["smtp_user"] or not row["smtp_pass_enc"]:
        raise HTTPException(
            status_code=400,
            detail="No email credentials configured. Enter your Gmail address and App Password in the Automator.",
        )

    smtp_user = row["smtp_user"]
    try:
        smtp_pass = _decrypt(row["smtp_pass_enc"])
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Stored credentials are corrupted. Please re-enter your email and App Password.",
        )

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = req.to_email
    msg["Subject"] = req.subject
    msg.attach(MIMEText(req.body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, req.to_email, msg.as_string())
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(
            status_code=400,
            detail="Gmail authentication failed. Make sure you're using an App Password, not your regular password.",
        )
    except smtplib.SMTPException as exc:
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}")

    logger.info("Automator sent email to %s for user %s", req.to_email, user["id"])
    return {"message": f"Email sent to {req.to_email}"}
