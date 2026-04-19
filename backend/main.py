"""
Tether — Hyper-local career agent API for Monroe, Louisiana.

PRIVACY CONTRACT
----------------
This service processes resume text entirely in memory. No resume content,
extracted text, or personally identifying information is written to disk,
persisted in any database, or included in log output. Each request is
processed and discarded. Do not add logging calls that capture `raw_text`,
`clean_text`, or any derivative of the uploaded file's content.
"""

import io
import logging
import os

from dotenv import load_dotenv

# Load .env before any other module reads os.environ
load_dotenv()

# ---------------------------------------------------------------------------
# Startup guard — fail loudly if the API key is missing so the error is
# obvious during local dev rather than surfacing as a cryptic 502 later.
# ---------------------------------------------------------------------------
if not os.environ.get("ANTHROPIC_API_KEY"):
    raise RuntimeError(
        "\n\n"
        "  ANTHROPIC_API_KEY is not set.\n"
        "  Copy backend/.env.example → backend/.env and add your key.\n"
        "  Never commit .env to version control.\n"
    )

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pypdf import PdfReader
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.middleware.cors import CORSMiddleware

from employers import load_employers, select_candidates
from llm import generate_report as llm_generate_report
from database import init_db
from auth_router import router as auth_router
from chat_router import router as chat_router
from report_router import router as report_router
from jobs_router import router as jobs_router
from outreach_router import router as outreach_router
from contacts_router import router as contacts_router
from security import (
    limiter,
    sanitize_field,
    sanitize_resume_text,
    validate_form_inputs,
    validate_pdf_upload,
    validate_resume_text,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS — read allowed origins from env; never permit wildcard
# ---------------------------------------------------------------------------
_raw_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

if "*" in ALLOWED_ORIGINS:
    raise RuntimeError(
        "Wildcard '*' is not permitted in CORS_ORIGINS. "
        "Specify explicit origins, e.g. http://localhost:3000."
    )

# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to every outgoing response."""

    # Swagger UI loads JS/CSS from a CDN — relax CSP only for the docs paths.
    _DOCS_PATHS = {"/docs", "/openapi.json"}

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        if request.url.path in self._DOCS_PATHS:
            # Permit the CDN assets Swagger UI requires
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
                "img-src 'self' data:; "
                "worker-src blob:;"
            )
        else:
            response.headers["Content-Security-Policy"] = "default-src 'self'"

        return response


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Tether Career Agent API",
    description="Hyper-local career matching for Monroe, Louisiana.",
    version="0.1.0",
    # Disable the default /redoc to reduce attack surface in production.
    redoc_url=None,
)

# Rate limiter state (required by slowapi)
app.state.limiter = limiter

# Middleware — order matters: security headers wrap everything, CORS is inner.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# ---------------------------------------------------------------------------
# Exception handlers
# All handlers return {"error": "..."} — stack traces never reach the client.
# ---------------------------------------------------------------------------

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "Too many requests. Please wait a moment and try again."},
        headers={"Retry-After": "60"},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # detail is set explicitly in our raise HTTPException() calls — always a str.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(exc.detail)},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Log the real error server-side; return a generic message to the client.
    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "An unexpected error occurred. Please try again."},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(report_router)
app.include_router(jobs_router)
app.include_router(outreach_router)
app.include_router(contacts_router)


@app.get("/health")
@limiter.limit("30/minute")
async def health(request: Request):
    return {"status": "ok"}


@app.post("/api/generate-report")
@limiter.limit("5/minute")
async def generate_report(
    request: Request,
    resume: UploadFile,
    major: str = Form(...),
    year: str = Form(...),
):
    """
    Accept a PDF resume + form fields; return a structured career analysis.

    Steps
    -----
    1.  Read and validate the uploaded file (size, MIME type, magic bytes).
    2.  Extract text from the PDF entirely in memory — no disk writes.
    3.  Validate extracted text length (too short = scanned PDF; too long = not a resume).
    4.  Sanitize form inputs (strip control characters).
    5.  Validate year/major values.
    6.  Strip prompt injection patterns from resume text.
    7.  Select the 10 most relevant employers via keyword matching.
    8.  Call Claude; Claude picks its top 5 and returns the full JSON report.
    """

    # ------------------------------------------------------------------
    # 1. Read file bytes
    # ------------------------------------------------------------------
    content = await resume.read()

    # ------------------------------------------------------------------
    # 2. File validation — raises HTTP 413 or 415 on failure
    # ------------------------------------------------------------------
    validate_pdf_upload(resume, content)

    # ------------------------------------------------------------------
    # 3. Extract text in memory — no disk writes, ever
    # ------------------------------------------------------------------
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        raw_text = "\n".join(pages)
    except Exception as exc:
        logger.error("PDF text extraction failed", exc_info=exc)
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not read the PDF. Please ensure it is a text-based "
                "(not scanned or image-only) resume and try again."
            ),
        )
    finally:
        # Drop raw bytes immediately after extraction.
        del content

    # ------------------------------------------------------------------
    # 4. Validate extracted text length — raises HTTP 422 on failure
    # ------------------------------------------------------------------
    validate_resume_text(raw_text)

    # ------------------------------------------------------------------
    # 5. Sanitize and validate form inputs
    # ------------------------------------------------------------------
    major = sanitize_field(major)
    year = sanitize_field(year, max_length=20)
    validate_form_inputs(major, year)  # raises HTTP 422 on invalid values

    # ------------------------------------------------------------------
    # 6. Prompt injection guard — strips patterns, logs warning if found
    # ------------------------------------------------------------------
    clean_text = sanitize_resume_text(raw_text)
    del raw_text  # discard the unsanitized copy

    # ------------------------------------------------------------------
    # 7. Select 10 candidates via keyword matching; Claude picks its top 5.
    #    <RESUME_TEXT> wrapping is applied inside generate_report() so the
    #    wrapped string never persists beyond that function's stack frame.
    # ------------------------------------------------------------------
    candidates = select_candidates(major, load_employers())

    # ------------------------------------------------------------------
    # 8. Call Claude for structured analysis
    # ------------------------------------------------------------------
    try:
        result = llm_generate_report(clean_text, major, year.lower(), candidates)
    except ValueError:
        # Malformed JSON after retry — already logged in llm.py
        raise HTTPException(
            status_code=502,
            detail="Career analysis returned an unexpected format. Please try again.",
        )
    except Exception:
        # APIError, connection error, rate limit — already logged in llm.py
        raise HTTPException(
            status_code=502,
            detail="Career analysis service is temporarily unavailable. Please try again shortly.",
        )
    finally:
        del clean_text  # ensure cleanup regardless of outcome

    return result


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("Tether backend started — SQLite DB initialised")
