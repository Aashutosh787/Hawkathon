"""
test_local.py — Smoke-test generate_report() without spinning up the HTTP server.

Run from the backend/ directory with the venv active:
    python test_local.py
"""

import json
import os
import sys
from pathlib import Path

# Ensure backend/ is on the path regardless of where this is invoked from
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from employers import load_employers, select_candidates
from llm import generate_report
from security import sanitize_resume_text

# ---------------------------------------------------------------------------
# Fake resume — Junior Nursing student at ULM
# ---------------------------------------------------------------------------

SAMPLE_RESUME = """
ASHLEY MARIE THIBODAUX
Monroe, Louisiana 71201 | (318) 555-0142 | ashley.thibodaux@ulm.edu
LinkedIn: linkedin.com/in/ashleythibodaux

EDUCATION
University of Louisiana Monroe — Bachelor of Science in Nursing (BSN)
Expected Graduation: May 2026 | GPA: 3.6 / 4.0
Relevant Coursework: Fundamentals of Nursing, Pathophysiology, Pharmacology,
  Medical-Surgical Nursing I & II, Maternal-Newborn Nursing, Pediatric Nursing,
  Mental Health Nursing, Community Health Nursing

CLINICAL ROTATIONS (750 hours completed)
St. Francis Medical Center, Monroe, LA — Med-Surg Unit (160 hours, Fall 2023)
  • Provided direct patient care for 4-6 adult patients per shift under RN supervision
  • Administered oral and IV medications, performed wound assessments and dressing changes
  • Documented patient vitals and progress notes in Epic EHR

Ochsner LSU Health Monroe — Labor & Delivery / Postpartum (120 hours, Spring 2024)
  • Assisted with fetal monitoring, postpartum assessments, and newborn care
  • Supported patients through pain management education and breastfeeding instruction

Glenwood Regional Medical Center, West Monroe, LA — Pediatric Unit (80 hours, Fall 2024)
  • Performed age-appropriate assessments for pediatric patients (newborn–17 years)
  • Collaborated with interdisciplinary team during morning rounds

CERTIFICATIONS & SKILLS
  • Basic Life Support (BLS) — American Heart Association (expires Dec 2025)
  • ACLS — in progress (scheduled Feb 2025)
  • Epic EHR (proficient), Microsoft Office, point-of-care testing (glucometry, INR)
  • Spanish — conversational

WORK EXPERIENCE
Patient Care Technician (PRN) — St. Francis Medical Center, Monroe, LA
June 2023 – Present
  • Assist RNs with ADLs, vital sign collection, and patient transport on 32-bed med-surg floor
  • Maintain 98% shift documentation accuracy per unit quality metrics

Peer Tutor — ULM Learning Center
August 2022 – May 2023
  • Tutored 12 first- and second-year nursing students in Anatomy & Physiology and Pharmacology

ORGANIZATIONS
  • Student Nurses Association of Louisiana (SNAL) — Vice President, ULM Chapter
  • Sigma Theta Tau International Honor Society of Nursing — inducted April 2024
  • ULM Volunteer Health Clinic — 40+ hours of community health screenings

REFERENCES
Available upon request.
"""


def main():
    print("=" * 60)
    print("Tether — generate_report() smoke test")
    print("Major: Nursing  |  Year: junior")
    print("=" * 60)

    # Step 1 — sanitize (mirrors main.py pipeline)
    clean_text = sanitize_resume_text(SAMPLE_RESUME)
    print(f"\n[1] Resume sanitized. Character count: {len(clean_text)}")

    # Step 2 — select candidates
    all_employers = load_employers()
    candidates = select_candidates("Nursing", all_employers)
    print(f"[2] Candidates selected: {len(candidates)}")
    for e in candidates:
        print(f"    • {e['id']} ({e['industry']}, hiring={e['hiring_likelihood']})")

    # Step 3 — call Claude
    print("\n[3] Calling generate_report()... (this takes ~10-20 seconds)\n")
    result = generate_report(clean_text, "Nursing", "junior", candidates)

    # Step 4 — print output
    print("=" * 60)
    print("RESPONSE")
    print("=" * 60)
    print(json.dumps(result, indent=2))

    # Step 5 — basic field validation
    print("\n" + "=" * 60)
    print("FIELD CHECK")
    print("=" * 60)
    expected_keys = {
        "student_summary", "top_employers", "salary_trajectory",
        "skill_gaps", "action_plan_90_days",
    }
    present = set(result.keys())
    missing = expected_keys - present
    extra   = present - expected_keys

    print(f"  Expected keys present : {sorted(expected_keys & present)}")
    if missing:
        print(f"  MISSING keys          : {sorted(missing)}")
    if extra:
        print(f"  Unexpected extra keys : {sorted(extra)}")

    employers_count = len(result.get("top_employers", []))
    skill_gaps_count = len(result.get("skill_gaps", []))
    action_count = len(result.get("action_plan_90_days", []))

    checks = [
        ("top_employers == 5",       employers_count == 5,           f"got {employers_count}"),
        ("skill_gaps 2-3",           2 <= skill_gaps_count <= 3,     f"got {skill_gaps_count}"),
        ("action_plan_90_days 4-5",  4 <= action_count <= 5,         f"got {action_count}"),
        ("no missing top-level keys", not missing,                    f"missing: {missing}"),
    ]

    all_passed = True
    for label, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"  [{status}] {label:<35} {detail}")

    print()
    if all_passed:
        print("All checks passed.")
    else:
        print("One or more checks failed — review the prompt in llm.py.")
        sys.exit(1)


if __name__ == "__main__":
    main()
