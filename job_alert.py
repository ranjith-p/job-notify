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

# German-language-requirement exclusion. Deliberately TIGHT proximity
# matching: only excludes when a level word (C1/C2/fluent/native/etc.)
# sits immediately next to "German"/"Deutsch" — not just anywhere in the
# same text. This avoids false positives like "English C1, German B2"
# (where the C1 belongs to English, not German).
_LEVEL_WORDS = r"(?:c1|c2|fluent\w*|nativ\w*|muttersprach\w*|verhandlungssicher\w*)"
_LANG_WORDS = r"(?:german|deutsch\w*)"
_GAP = r"[\s:()\-]{0,5}"  # allowed separators between language & level — NOT commas
_GERMAN_LEVEL_RE = re.compile(
    rf"\b{_LANG_WORDS}\b{_GAP}\b{_LEVEL_WORDS}\b"
    rf"|\b{_LEVEL_WORDS}\b{_GAP}\b{_LANG_WORDS}\b",
    re.IGNORECASE,
)

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
    description = job.get("description", "")
    combined = f"{title} {description}"

    if _EXCLUDE_TITLE_RE.search(title):
        return "internship/working-student role"

    if _GERMAN_LEVEL_RE.search(combined):
        return "requires German C1+/fluent/native"

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


def send_telegram(label: str, job: dict) -> None:
    title = job.get("title", "Untitled role")
    company = job.get("company", {}).get("display_name", "Unknown company")
    location = job.get("location", {}).get("display_name", "Unknown location")
    url = job.get("redirect_url", "")
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")

    salary_line = ""
    if salary_min and salary_max:
        salary_line = f"\n💰 {salary_min:,.0f}–{salary_max:,.0f}"

    # HTML parse mode + explicit escaping is far more robust than Telegram's
    # legacy Markdown, which breaks (400 error) on unescaped *, _, [, ], (, )
    # etc. — all common in job titles like "(all genders)" or "AI/ML".
    text = (
        f"{html.escape(label)} — New role\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{html.escape(company)} · {html.escape(location)}{salary_line}\n\n"
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
            send_telegram(cfg["label"], job)
            print(f"[{name}] Sent alert: {job.get('title')}")
        except Exception as exc:  # noqa: BLE001
            # Critical: never let one bad message crash the whole run —
            # that would skip save_state() below and reset the cursor to
            # square one, causing every job to be resent next run. Log and
            # move on; the cursor still advances since recent_ids/newest_iso
            # were already updated in the filtering loop above.
            print(f"[{name}] ERROR sending Telegram message for "
                  f"'{job.get('title')}': {exc}", file=sys.stderr)

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
