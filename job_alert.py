"""
Data science job alert bot.

Runs two searches against the Adzuna API:
  1. Germany-wide
  2. Berlin-only

For each, finds jobs newer than the last run (using a timestamp cursor +
a small rolling set of recently-seen IDs as a safety net against ties/reposts)
and sends a Telegram message for each new listing.

State is stored in state/germany.json and state/berlin.json so it persists
between runs (the GitHub Actions workflow commits these back to the repo).
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.1-8b-instant and llama-3.3-70b-versatile were deprecated by Groq
# on 2026-06-17. gpt-oss-20b is the recommended fast/cheap replacement.
GROQ_MODEL = "openai/gpt-oss-20b"

# Condensed from the candidate's full career history — kept short
# deliberately so each scoring call stays cheap and fast.
CANDIDATE_PROFILE = """
Data Scientist with experience spanning banking/fintech, credit risk,
marketing analytics, geospatial analytics, and applied AI/LLM products.

Key experience:
- Built ML models for a Fortune 500 US bank to optimize mortgage customer
  acquisition (propensity modeling, A/B testing, ~15% cost reduction across
  138M households); production pipeline scoring 300M+ individuals weekly.
- New-to-credit underwriting models using alternative data, adopted by
  10+ banks, 5,000+ customer onboardings/month.
- Retail/banking forecasting (FMCG store sales, ATM transaction volume)
  using geospatial features, 82-85% forecast accuracy.
- Enterprise geospatial/location-intelligence SaaS platform: PySpark ETL
  on 300M+ records, custom geocoding engine (16M+ addresses), used by
  10+ enterprise clients.
- Master's thesis: explainable ML (LightGBM + SHAP) on 19.4M HMDA mortgage
  records combined with 6 national datasets, studying neighborhood
  mortgage dynamics across interest-rate cycles.
- Personal project: AI-native adaptive language-learning platform using
  RAG + LLMs with persistent learner memory.
- Core skills: Python, ML/predictive modeling, experimentation (A/B
  testing), feature engineering, PySpark/data engineering, explainable AI
  (SHAP), geospatial analytics, stakeholder collaboration, product thinking.

Target roles: Data Science, Decision Science, Applied AI/ML, Product
Analytics, AI Product Development. Currently based in Berlin, Germany.
""".strip()

# Adzuna country code for Germany
ADZUNA_COUNTRY = "de"

# Keywords (OR'd together) — edit freely
KEYWORDS = [
    "data scientist",
    "data science",
    "machine learning engineer",
    "ML engineer",
    "applied scientist",
    "research scientist",
    "data analyst",
]

# Title keywords that mean "skip this" — internships / working-student /
# similar non-full-employee roles. Word-boundary matched so "intern"
# doesn't false-match inside "international".
EXCLUDE_TITLE_PATTERNS = [
    r"\bpraktikum\b",
    r"\bpraktikant\w*\b",
    r"\bwerkstudent\w*\b",
    r"\bintern\b",
    r"\binternship\b",
    r"\bworking student\b",
]
_EXCLUDE_TITLE_RE = re.compile("|".join(EXCLUDE_TITLE_PATTERNS), re.IGNORECASE)

# How many recent IDs to remember per search, as a tie-break safety net
RECENT_ID_CAP = 50

# Results per page to pull each run (Adzuna max is 50)
RESULTS_PER_PAGE = 50

STATE_DIR = Path(__file__).parent / "state"

SEARCHES = {
    "germany": {
        "label": "🇩🇪 Germany",
        "location": None,  # no 'where' filter = whole country
        "state_file": STATE_DIR / "germany.json",
    },
    "berlin": {
        "label": "📍 Berlin",
        "location": "Berlin",
        "state_file": STATE_DIR / "berlin.json",
    },
}


def should_exclude(job: dict) -> str | None:
    """Return a short reason string if the job should be skipped, else None."""
    title = job.get("title", "")

    if _EXCLUDE_TITLE_RE.search(title):
        return "internship/working-student role"

    # Adzuna's contract_time/contract_type fields are only populated for a
    # minority of German listings — requiring full_time=1 silently dropped
    # every untagged posting too. Instead, only exclude jobs EXPLICITLY
    # tagged part-time or contract/temporary; untagged jobs (the majority)
    # are kept rather than assumed to be excluded.
    if job.get("contract_time") == "part_time":
        return "explicitly tagged part-time"

    if job.get("contract_type") == "contract":
        return "explicitly tagged temporary/contract"

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"last_seen_iso": "1970-01-01T00:00:00Z", "recent_ids": []}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def fetch_jobs(location: str | None) -> list[dict]:
    """
    Query Adzuna once per keyword phrase (exact-phrase match) and merge
    the results, deduped by job ID, sorted newest first.

    Note: Adzuna's `what_or` parameter ORs individual *words*, not
    phrases — passing multi-word keywords through it causes false
    matches on any single word (e.g. "engineer" alone). Querying each
    phrase separately with `what_phrase` avoids that.
    """
    url = f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/1"
    seen_ids = set()
    merged: list[dict] = []

    for keyword in KEYWORDS:
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "results_per_page": RESULTS_PER_PAGE,
            "what_phrase": keyword,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if location:
            params["where"] = location

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        for job in resp.json().get("results", []):
            job_id = str(job.get("id"))
            if job_id and job_id not in seen_ids:
                seen_ids.add(job_id)
                merged.append(job)
        time.sleep(0.3)  # be polite to Adzuna's rate limits

    return merged


def send_telegram(label: str, job: dict, match: dict) -> None:
    title = job.get("title", "Untitled role")
    company = job.get("company", {}).get("display_name", "Unknown company")
    location = job.get("location", {}).get("display_name", "Unknown location")
    url = job.get("redirect_url", "")
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")

    salary_line = ""
    if salary_min and salary_max:
        salary_line = f"\n💰 {salary_min:,.0f}–{salary_max:,.0f}"

    score = match.get("match_score")
    score_line = ""
    if score:
        score_line = f"\n🎯 Match: {html.escape(str(score))}/10 — {html.escape(match.get('match_reason', ''))}"

    german_line = f"\n🇩🇪 German: {html.escape(match.get('german_requirement', 'Not mentioned'))}"

    # HTML parse mode + explicit escaping is far more robust than Telegram's
    # legacy Markdown, which breaks (400 error) on unescaped *, _, [, ], (, )
    # etc. — all common in job titles like "(all genders)" or "AI/ML".
    text = (
        f"{html.escape(label)} — New role\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{html.escape(company)} · {html.escape(location)}{salary_line}"
        f"{score_line}{german_line}\n\n"
        f"{html.escape(url)}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api_url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def score_job_match(job: dict) -> dict:
    """
    Ask Groq to score how well this job matches the candidate profile,
    and to extract the actual German language requirement (if any) as
    a short human-readable note.

    Returns a dict like:
        {"match_score": 8, "match_reason": "...", "german_requirement": "..."}
    On any failure, returns a safe fallback dict rather than raising —
    callers should never let a scoring failure block sending the job or
    persisting state.
    """
    title = job.get("title", "")
    description = job.get("description", "")

    system_prompt = (
        "You are a job-matching assistant. Given a candidate profile and a "
        "job posting, respond with STRICT JSON only — no markdown, no code "
        "fences, no extra text — in exactly this schema:\n"
        '{"match_score": <integer 1-10>, "match_reason": "<one short '
        'sentence, under 20 words, on why this score>", "german_requirement": '
        '"<one short phrase describing the German language requirement, e.g. '
        '\'Not mentioned\', \'Not mandatory, English OK\', \'B2 required\', '
        '\'C1 fluent required for client communication\'>"}\n'
        "Base match_score on how well the job aligns with the candidate's "
        "actual experience and target roles — not just keyword overlap."
    )
    user_prompt = (
        f"CANDIDATE PROFILE:\n{CANDIDATE_PROFILE}\n\n"
        f"JOB POSTING:\nTitle: {title}\nDescription: {description}"
    )

    resp = requests.post(
        GROQ_API_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]

    # Defensive parsing: strip accidental code fences even though we
    # requested json_object mode, in case the model wraps it anyway.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    parsed = json.loads(cleaned)
    return {
        "match_score": int(parsed.get("match_score", 0)),
        "match_reason": str(parsed.get("match_reason", "")).strip(),
        "german_requirement": str(parsed.get("german_requirement", "Not mentioned")).strip(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_search(name: str, cfg: dict) -> None:
    state = load_state(cfg["state_file"])
    print(f"[{name}] DEBUG loaded state from {cfg['state_file']}: {state}")
    last_seen = parse_iso(state["last_seen_iso"])
    recent_ids = set(state["recent_ids"])

    jobs = fetch_jobs(cfg["location"])

    # Adzuna already sorts by date desc; walk oldest->newest for correct
    # cursor progression, but only act on genuinely new ones.
    new_jobs = []
    newest_iso = state["last_seen_iso"]

    for job in sorted(jobs, key=lambda j: j.get("created", "")):
        job_id = str(job.get("id"))
        created = job.get("created", "")
        if not created or not job_id:
            continue
        created_dt = parse_iso(created)

        is_new_by_time = created_dt > last_seen
        is_new_by_id = job_id not in recent_ids

        if is_new_by_time and is_new_by_id:
            recent_ids.add(job_id)
            if created > newest_iso:
                newest_iso = created

            exclude_reason = should_exclude(job)
            if exclude_reason:
                print(f"[{name}] Skipped ({exclude_reason}): {job.get('title')}")
                continue

            new_jobs.append(job)

    for job in new_jobs:
        try:
            match = score_job_match(job)
        except Exception as exc:  # noqa: BLE001
            # A scoring failure should never block the job from being sent
            # or block state persistence — fall back to no score shown.
            print(f"[{name}] WARNING scoring failed for "
                  f"'{job.get('title')}': {exc}", file=sys.stderr)
            match = {"match_score": None, "match_reason": "", "german_requirement": "Unknown"}

        try:
            send_telegram(cfg["label"], job, match)
            print(f"[{name}] Sent alert: {job.get('title')} "
                  f"(match={match.get('match_score')})")
        except Exception as exc:  # noqa: BLE001
            # Critical: never let one bad message crash the whole run —
            # that would skip save_state() below and reset the cursor to
            # square one, causing every job to be resent next run. Log and
            # move on; the cursor still advances since recent_ids/newest_iso
            # were already updated in the filtering loop above.
            print(f"[{name}] ERROR sending Telegram message for "
                  f"'{job.get('title')}': {exc}", file=sys.stderr)
        time.sleep(0.3)  # be polite to Groq's rate limits

    # Trim recent_ids to the cap (keep most recently added — since sets
    # don't preserve order, just cap by re-deriving from the current jobs
    # payload order, newest first).
    ordered_recent = [str(j["id"]) for j in jobs if str(j.get("id")) in recent_ids]
    trimmed = ordered_recent[:RECENT_ID_CAP] if ordered_recent else list(recent_ids)[:RECENT_ID_CAP]

    new_state = {
        "last_seen_iso": newest_iso,
        "recent_ids": trimmed,
    }
    print(f"[{name}] DEBUG about to write state: {new_state}")
    save_state(cfg["state_file"], new_state)
    print(f"[{name}] DEBUG state file now on disk: {cfg['state_file'].read_text()}")

    if not new_jobs:
        print(f"[{name}] No new jobs this run.")


def main() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    for name, cfg in SEARCHES.items():
        try:
            run_search(name, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] ERROR: {exc}", file=sys.stderr)
            # Don't let one search's failure kill the other
            continue


if __name__ == "__main__":
    main()
