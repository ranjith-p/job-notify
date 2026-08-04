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

import json
import os
import sys
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
    """Query Adzuna for the keyword list, sorted newest-first."""
    url = f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PER_PAGE,
        "what_or": " ".join(KEYWORDS),  # OR search across keyword list
        "sort_by": "date",
        "content-type": "application/json",
    }
    if location:
        params["where"] = location

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


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

    text = (
        f"{label} — New role\n\n"
        f"*{title}*\n"
        f"{company} · {location}{salary_line}\n\n"
        f"{url}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api_url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
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
            new_jobs.append(job)
            recent_ids.add(job_id)
            if created > newest_iso:
                newest_iso = created

    for job in new_jobs:
        send_telegram(cfg["label"], job)
        print(f"[{name}] Sent alert: {job.get('title')}")

    # Trim recent_ids to the cap (keep most recently added — since sets
    # don't preserve order, just cap by re-deriving from the current jobs
    # payload order, newest first).
    ordered_recent = [str(j["id"]) for j in jobs if str(j.get("id")) in recent_ids]
    trimmed = ordered_recent[:RECENT_ID_CAP] if ordered_recent else list(recent_ids)[:RECENT_ID_CAP]

    save_state(cfg["state_file"], {
        "last_seen_iso": newest_iso,
        "recent_ids": trimmed,
    })

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
