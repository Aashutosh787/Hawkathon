"""
security.py — Input validation, prompt injection hardening, and rate limiting.

Nothing in this module writes, logs, or persists user resume content.
"""

import re
import logging

from fastapi import HTTPException, UploadFile
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter instance — imported and mounted by main.py
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Upload constraints
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 2 * 1024 * 1024   # 2 MB
MIN_RESUME_CHARS = 100
MAX_RESUME_CHARS = 20_000
PDF_MAGIC = b"%PDF-"

# ---------------------------------------------------------------------------
# Prompt injection patterns
# Matched case-insensitively; occurrences are stripped and a warning is logged.
# The upload is NOT rejected — students may have these phrases legitimately
# (e.g., "I was told to ignore previous instructions and design a new course").
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+the\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"new\s+instructions\s*:", re.IGNORECASE),
    re.compile(r"#{3,}", re.IGNORECASE),           # ### or more
    re.compile(r"```\s*system", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Form field constraints
# ---------------------------------------------------------------------------

MAX_MAJOR_LENGTH = 120

VALID_YEARS = {"freshman", "sophomore", "junior", "senior"}


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def validate_pdf_upload(file: UploadFile, content: bytes) -> None:
    """
    Reject the upload if:
    - File exceeds 2 MB (413)
    - MIME type is not application/pdf (415)
    - File does not begin with the %PDF- magic bytes (415)
    """
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum allowed size is 2 MB.",
        )

    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=415,
            detail="Only PDF files are accepted. Please upload a .pdf resume.",
        )

    if not content.startswith(PDF_MAGIC):
        raise HTTPException(
            status_code=415,
            detail="File does not appear to be a valid PDF.",
        )


def validate_resume_text(text: str) -> None:
    """
    Reject if extracted text is suspiciously short (likely a scanned/image PDF)
    or unusually large (may not be a resume).
    """
    length = len(text.strip())
    if length < MIN_RESUME_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not extract enough text from the PDF. "
                "Please upload a text-based (not scanned/image) resume."
            ),
        )
    if length > MAX_RESUME_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Extracted text exceeds the maximum allowed size. "
                "Please upload a standard single- or two-page resume."
            ),
        )


def validate_form_inputs(major: str, year: str) -> None:
    """Validate the major and year form fields."""
    if not major or not major.strip():
        raise HTTPException(status_code=422, detail="The 'major' field is required.")
    if len(major) > MAX_MAJOR_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"The 'major' field must be {MAX_MAJOR_LENGTH} characters or fewer.",
        )
    if year.lower() not in VALID_YEARS:
        raise HTTPException(
            status_code=422,
            detail=f"'year' must be one of: {', '.join(sorted(VALID_YEARS))}.",
        )


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def sanitize_field(value: str, max_length: int = MAX_MAJOR_LENGTH) -> str:
    """
    Strip control characters and newlines from short form fields before they
    are interpolated into the LLM prompt.
    """
    cleaned = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", value[:max_length])
    return cleaned.strip()


def sanitize_resume_text(text: str) -> str:
    """
    Scan resume text for prompt-injection patterns.
    Any matches are replaced with [REDACTED] and a warning is logged.
    The sanitised text is returned — the request continues normally.
    """
    sanitized = text
    matched: list[str] = []

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(sanitized):
            matched.append(pattern.pattern)
            sanitized = pattern.sub("[REDACTED]", sanitized)

    if matched:
        # Log the number of patterns — NOT the surrounding text — to avoid
        # inadvertently persisting PII-adjacent content in server logs.
        logger.warning(
            "Prompt injection sanitization applied to uploaded resume. "
            "Patterns matched: %d",
            len(matched),
        )

    return sanitized


def wrap_resume_for_llm(text: str) -> str:
    """
    Wrap resume text in explicit delimiters so that Claude's system prompt
    can instruct it to treat the enclosed content as untrusted user data,
    never as instructions.
    """
    return f"<RESUME_TEXT>\n{text}\n</RESUME_TEXT>"
