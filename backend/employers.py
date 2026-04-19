"""
employers.py — Loads monroe_employers.json and pre-ranks employers by
relevance to a student's declared major before the LLM call.

Pre-ranking trims the full 25-employer list to a manageable subset so the
Claude prompt stays focused. Claude does the real semantic matching.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).parent.parent / "data" / "monroe_employers.json"

# ---------------------------------------------------------------------------
# Internal-only fields stripped before data is sent to the LLM
# (verification metadata is not meaningful to students or Claude)
# ---------------------------------------------------------------------------
_STRIP_FIELDS = {"needs_verification", "verification_note"}

# ---------------------------------------------------------------------------
# Lazy-loaded singleton — file is read once on first access
# ---------------------------------------------------------------------------
_employers_cache: list[dict[str, Any]] | None = None


def load_employers() -> list[dict[str, Any]]:
    """
    Return the full employer list, loading from disk on first call and
    caching in memory for all subsequent calls.
    """
    global _employers_cache
    if _employers_cache is None:
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as fh:
                _employers_cache = json.load(fh)
            logger.info("Loaded %d employers from %s", len(_employers_cache), DATA_PATH)
        except FileNotFoundError:
            logger.error("monroe_employers.json not found at %s", DATA_PATH)
            raise
    return _employers_cache


# ---------------------------------------------------------------------------
# Major → preferred industry mapping
# Keys are substrings matched case-insensitively against the student's major.
# Order matters: more specific keys should appear first.
# ---------------------------------------------------------------------------
_MAJOR_INDUSTRY_MAP: list[tuple[str, list[str]]] = [
    # Healthcare
    ("nursing",             ["healthcare"]),
    ("pre-med",             ["healthcare"]),
    ("pre med",             ["healthcare"]),
    ("physician assistant", ["healthcare"]),
    ("pharmacy",            ["healthcare"]),
    ("allied health",       ["healthcare"]),
    ("health admin",        ["healthcare", "finance"]),
    ("health information",  ["healthcare", "tech"]),
    ("physical therapy",    ["healthcare"]),
    ("occupational therapy",["healthcare"]),
    ("social work",         ["healthcare", "education"]),
    ("public health",       ["healthcare"]),
    # Tech / Engineering
    ("computer science",    ["tech"]),
    ("computer engineering",["tech", "manufacturing"]),
    ("information technol", ["tech"]),
    ("information system",  ["tech", "finance"]),
    ("cyber",               ["tech"]),
    ("software",            ["tech"]),
    ("electrical engineer", ["energy", "tech", "manufacturing"]),
    ("mechanical engineer", ["manufacturing", "energy"]),
    ("civil engineer",      ["tech", "manufacturing"]),
    ("industrial engineer", ["manufacturing", "logistics"]),
    # Business / Finance
    ("accounting",          ["finance"]),
    ("finance",             ["finance"]),
    ("economics",           ["finance"]),
    ("business admin",      ["finance", "retail", "tech"]),
    ("management",          ["finance", "logistics", "retail"]),
    ("marketing",           ["retail", "tech", "finance"]),
    ("entrepreneurship",    ["tech", "retail", "finance"]),
    # Energy
    ("petroleum",           ["energy", "manufacturing"]),
    ("chemical engineer",   ["energy", "manufacturing"]),
    ("environmental",       ["energy", "agriculture"]),
    # Agriculture
    ("agronomy",            ["agriculture"]),
    ("agricultural",        ["agriculture", "finance"]),
    ("agribusiness",        ["agriculture", "finance"]),
    # Logistics / Supply Chain
    ("logistics",           ["logistics", "manufacturing"]),
    ("supply chain",        ["logistics", "manufacturing"]),
    ("operations",          ["logistics", "manufacturing"]),
    # Education
    ("education",           ["education"]),
    ("kinesiology",         ["healthcare", "education"]),
    # Sciences (broad — keep after specifics)
    ("biology",             ["healthcare", "energy", "agriculture"]),
    ("chemistry",           ["healthcare", "energy", "manufacturing"]),
    ("mathematics",         ["tech", "finance"]),
    ("pre-law",             ["finance", "education"]),
]

_HIRING_WEIGHT: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


def _preferred_industries(major: str) -> list[str]:
    """Return the ordered preferred industry list for a given major string."""
    major_lower = major.lower()
    for keyword, industries in _MAJOR_INDUSTRY_MAP:
        if keyword in major_lower:
            return industries
    return []  # No strong preference — all industries eligible


def _score(employer: dict[str, Any], preferred: list[str]) -> int:
    """
    Heuristic relevance score used only for pre-filtering.
    Claude performs the real semantic matching.
    """
    score = 0

    # Hiring likelihood (most impactful — a 'high' employer always surfaces)
    score += _HIRING_WEIGHT.get(employer.get("hiring_likelihood", "low"), 0) * 10

    # Industry match
    industry = employer.get("industry", "")
    if preferred and industry in preferred:
        # Reward by position: first match in the preference list scores highest
        idx = preferred.index(industry)
        score += 30 - (idx * 5)

    # Minor penalty for unverified entries so verified employers surface first
    # when all else is equal
    if employer.get("needs_verification"):
        score -= 4

    return score


def get_candidate_employers(major: str, top_n: int = 15) -> list[dict[str, Any]]:
    """
    Return up to top_n employers pre-ranked by relevance to `major`.
    Internal-only fields are stripped before the list is sent to the LLM.
    """
    all_employers = load_employers()
    preferred = _preferred_industries(major)

    ranked = sorted(
        all_employers,
        key=lambda e: _score(e, preferred),
        reverse=True,
    )

    return [
        {k: v for k, v in emp.items() if k not in _STRIP_FIELDS}
        for emp in ranked[:top_n]
    ]


# ---------------------------------------------------------------------------
# Keyword-based candidate selection (used by the generate_report pipeline)
# ---------------------------------------------------------------------------

# Words too generic to be useful as match signals
_KW_STOP: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "for",
    "to", "with", "at", "by", "pre", "as", "is", "on",
})


def _major_keywords(major: str) -> frozenset[str]:
    """
    Extract meaningful tokens from a major string.
    Single-char tokens and stop words are discarded.

    Examples
    --------
    "Computer Science"          → {"computer", "science"}
    "Health Administration"     → {"health", "administration"}
    "Pre-Med / Biology"         → {"med", "biology"}
    """
    tokens = re.findall(r"[a-zA-Z]+", major.lower())
    return frozenset(t for t in tokens if t not in _KW_STOP and len(t) > 2)


def _kw_score(
    employer: dict[str, Any],
    keywords: frozenset[str],
    preferred: list[str],
) -> int:
    """
    Score an employer for keyword-based candidate selection.

    Signals (in descending weight):
    1. Industry preference derived from _MAJOR_INDUSTRY_MAP   (up to +20)
    2. Keyword hits in typical_roles and skills_valued         (+5 per hit)
    3. Keyword hit in industry string itself                   (+4 per hit)
    4. Hiring likelihood bonus                                 (up to +10)
    5. Small penalty for unverified entries                    (-3)
    """
    score = 0

    # 1. Industry preference
    industry = employer.get("industry", "")
    if preferred and industry in preferred:
        idx = preferred.index(industry)
        score += max(20 - idx * 4, 0)

    # Build a searchable corpus from the text-rich employer fields
    roles_blob = " ".join(employer.get("typical_roles", [])).lower()
    skills_blob = " ".join(employer.get("skills_valued", [])).lower()
    industry_blob = industry.lower()

    # 2 & 3. Keyword hits
    for kw in keywords:
        if kw in roles_blob or kw in skills_blob:
            score += 5
        if kw in industry_blob:
            score += 4

    # 4. Hiring likelihood
    score += {"high": 10, "medium": 5, "low": 0}.get(
        employer.get("hiring_likelihood", "low"), 0
    )

    # 5. Unverified penalty
    if employer.get("needs_verification"):
        score -= 3

    return score


def select_candidates(
    major: str,
    all_employers: list[dict[str, Any]],
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """
    Return up to `top_n` employers most relevant to `major` using keyword
    matching across industry, typical_roles, and skills_valued.

    This shortlist is passed to Claude, which picks the final top 5 and
    performs the full semantic analysis. Internal-only fields are stripped
    before the list leaves this module.

    Parameters
    ----------
    major : str
        Student's declared major string (already sanitized).
    all_employers : list
        Full employer list, typically from load_employers().
    top_n : int
        Maximum number of candidates to return (default 10).

    Returns
    -------
    list[dict]
        Ranked candidates with verification metadata removed.
    """
    keywords = _major_keywords(major)
    preferred = _preferred_industries(major)

    ranked = sorted(
        all_employers,
        key=lambda e: _kw_score(e, keywords, preferred),
        reverse=True,
    )

    return [
        {k: v for k, v in emp.items() if k not in _STRIP_FIELDS}
        for emp in ranked[:top_n]
    ]
