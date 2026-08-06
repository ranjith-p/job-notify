"""
Data science job alert bot.

Fetches Germany-wide AND Berlin-scoped Adzuna results (the Berlin-specific
query catches listings that fall outside the top-50-per-keyword cutoff on
the broader Germany-wide query), merges them into a single deduped list,
and runs ONE unified new/seen cursor across everything. Each job is
scored and sent exactly once, labeled 🇩🇪 Germany or 📍 Berlin based on
its actual location — this avoids the double-scoring/double-sending that
happened when Germany and Berlin were tracked as two independent searches
with separate state.

State is stored in state/combined.json so it persists between runs (the
GitHub Actions workflow commits this back to the repo).
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
# on 2026-06-17. Using the larger gpt-oss-120b (vs. the smaller -20b) for
# better accuracy on nuanced extraction — worth it now that job
# descriptions can be several thousand characters (see enrich_description).
GROQ_MODEL = "openai/gpt-oss-120b"

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
    r"\bduales?\s+studium\b",
    r"\bdual\s+study\b",
]
_EXCLUDE_TITLE_RE = re.compile("|".join(EXCLUDE_TITLE_PATTERNS), re.IGNORECASE)

# How many recent IDs to remember, as a tie-break safety net
RECENT_ID_CAP = 100

# Results per page to pull each run (Adzuna max is 50)
RESULTS_PER_PAGE = 50

STATE_DIR = Path(__file__).parent / "state"
STATE_FILE = STATE_DIR / "combined.json"

# We still query both scopes — Germany-wide AND Berlin-specific — because
# a broad nationwide query can push Berlin listings outside the
# top-50-per-keyword cutoff when there's a lot of nationwide volume, while
# the Berlin-scoped query (smaller pool) still catches them. Both feed
# into ONE merged, deduped list below.
FETCH_LOCATIONS = [None, "Berlin"]  # None = no 'where' filter = whole country


def label_for_job(job: dict) -> str:
    """Pick the display label based on the job's actual location."""
    display_name = job.get("location", {}).get("display_name", "")
    if "berlin" in display_name.lower():
        return "📍 Berlin"
    return "🇩🇪 Germany"


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


# Cap how much fetched page text we send to Groq, to bound token cost.
MAX_DESCRIPTION_CHARS = 8000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


# Cap how much fetched page text we send to Groq, to bound token cost.
# Generous cap since we now anchor extraction to the actual job content
# (see below) rather than risking it being eaten by page boilerplate.
MAX_DESCRIPTION_CHARS = 12000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def enrich_description(job: dict) -> str:
    """
    Return the best available job description text. Adzuna's API
    'description' field is frequently a truncated snippet — even when it
    LOOKS reasonably long (800-2000+ chars), it can still cut off before
    reaching requirements listed near the bottom of a posting (language
    requirements, in particular, tend to live there). So rather than
    gating on a length heuristic that let many truncated-but-not-short
    snippets slip through, we always attempt to fetch the full posting
    page.

    Naively stripping an entire page to text and taking the first N
    characters risks the cap being consumed by nav bars, cookie banners,
    and footers before ever reaching the real job description — so we
    anchor: locate where the known Adzuna snippet actually appears within
    the cleaned page text, and extract forward from THAT point, skipping
    past the boilerplate that usually precedes real content in the DOM.

    Falls back to the original snippet on any failure or if the anchor
    can't be found — this must never raise, since a fetch failure
    shouldn't block scoring or sending the job.
    """
    description = job.get("description", "") or ""
    url = job.get("redirect_url", "")
    if not url:
        return description

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobAlertBot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        text = _SCRIPT_STYLE_RE.sub(" ", resp.text)
        text = _HTML_TAG_RE.sub(" ", text)
        text = html.unescape(text)
        text = _WHITESPACE_RE.sub(" ", text).strip()

        # Anchor on a distinctive slice of the known snippet (skip the
        # very start, which is often generic like "About the role" —
        # a slice from partway in is more likely to be unique on the page).
        anchor = description[50:150].strip() if len(description) > 150 else description.strip()
        idx = text.find(anchor) if anchor else -1

        if idx != -1:
            candidate = text[idx:idx + MAX_DESCRIPTION_CHARS]
            print(f"DEBUG anchor found for '{job.get('title')}' at index {idx}; "
                  f"extracted {len(candidate)} chars from that point")
        else:
            candidate = text[:MAX_DESCRIPTION_CHARS]
            print(f"DEBUG anchor NOT found for '{job.get('title')}'; "
                  f"using first {len(candidate)} chars of page instead")

        if len(candidate) > len(description):
            return candidate
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING full-JD fetch failed for '{job.get('title')}': {exc}",
              file=sys.stderr)

    return description


def format_posted_time(created: str) -> str:
    """Format Adzuna's 'created' ISO timestamp as readable Berlin local time."""
    try:
        dt_utc = parse_iso(created)
        dt_berlin = dt_utc.astimezone(ZoneInfo("Europe/Berlin"))
        return dt_berlin.strftime("%d %b %Y, %I:%M %p")
    except Exception:  # noqa: BLE001
        return created or "Unknown"


def send_batch_header(count: int) -> None:
    """Send a single divider message marking the start of this run's batch,
    so it's easy to see where a new run's results begin in the chat
    without checking individual message timestamps."""
    now_berlin = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d %b %Y, %I:%M %p")
    text = (
        f"🔴🔴🔴 NEW BATCH — {count} ROLE{'S' if count != 1 else ''} 🔴🔴🔴\n"
        f"{now_berlin}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api_url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


def send_telegram(label: str, job: dict, match: dict) -> None:
    title = job.get("title", "Untitled role")
    company = job.get("company", {}).get("display_name", "Unknown company")
    location = job.get("location", {}).get("display_name", "Unknown location")
    url = job.get("redirect_url", "")
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")

    posted_line = f"\n\n🕒 Posted: {html.escape(format_posted_time(job.get('created', '')))}"

    salary_line = ""
    if salary_min and salary_max:
        salary_line = f"\n\n💰 {salary_min:,.0f}–{salary_max:,.0f}"

    score = match.get("match_score")
    score_line = ""
    if score:
        score_line = f"\n\n🎯 Match: {html.escape(str(score))}/10 — {html.escape(match.get('match_reason', ''))}"

    german_line = f"\n\n🇩🇪 German: {html.escape(match.get('german_requirement', 'Not mentioned'))}"

    exp = match.get("years_experience", "Not mentioned")
    exp_line = ""
    if exp and exp.lower() != "not mentioned":
        exp_line = f"\n\n📅 Experience: {html.escape(exp)}"

    # HTML parse mode + explicit escaping is far more robust than Telegram's
    # legacy Markdown, which breaks (400 error) on unescaped *, _, [, ], (, )
    # etc. — all common in job titles like "(all genders)" or "AI/ML".
    text = (
        f"{html.escape(label)} — New role\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{html.escape(company)} · {html.escape(location)}{posted_line}{salary_line}"
        f"{score_line}{german_line}{exp_line}\n\n"
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


def score_job_match(job: dict, description: str) -> dict:
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

    system_prompt = (
        "You are a job-matching assistant. Given a candidate profile and a "
        "job posting, respond with STRICT JSON only — no markdown, no code "
        "fences, no extra text — in exactly this schema:\n"
        '{"match_score": <integer 1-10>, "match_reason": "<one short '
        'sentence, under 20 words, on why this score>", "german_requirement": '
        '"<a short phrase precisely describing the German language '
        'situation>", "years_experience": "<the required years of '
        "experience exactly as stated in the posting, e.g. '5+ years', "
        "'1-3 years', '3+ years'; or 'Not mentioned' if the posting doesn't "
        'state a specific requirement>"}\n'
        "Base match_score and match_reason ONLY on how well the job aligns "
        "with the candidate's actual skills, experience, and target roles — "
        "not just keyword overlap. Do NOT factor the German language "
        "requirement into match_score or match_reason in any way — language "
        "is reported separately in german_requirement and must not affect "
        "or be mentioned in the other two fields.\n\n"
        "For german_requirement: carefully read the ENTIRE posting — "
        "language mentions are often a brief clause buried inside a longer "
        "sentence near the end (e.g. '...and enjoy X, Y, and ideally German' "
        "or 'Fluent German and good English skills'), not always a separate "
        "bullet point. Use exactly one of these forms:\n"
        "- 'Not mentioned' — ONLY if German is never referenced anywhere in "
        "the text at all.\n"
        "- 'Mentioned as a plus, not required' — German is explicitly named "
        "as optional/nice-to-have/ideal-but-not-required (e.g. 'ideally "
        "German', 'German is a plus').\n"
        "- '<level> required' — German is stated as required, e.g. 'B2 "
        "required', 'Fluent German required', 'C1 required for client "
        "communication'. If no specific CEFR level is given but fluency is "
        "clearly required, say 'Fluent German required'."
    )
    user_prompt = (
        f"CANDIDATE PROFILE:\n{CANDIDATE_PROFILE}\n\n"
        f"JOB POSTING:\nTitle: {title}\nDescription: {description}"
    )

    request_body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    # Retry on rate limits (429) with backoff — with the larger model and
    # burst runs sending many jobs at once, hitting Groq's per-minute
    # limit is expected occasionally; a short wait-and-retry recovers
    # instead of just giving up and showing no score.
    max_attempts = 4
    resp = None
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=request_body,
            timeout=30,
        )
        if resp.status_code != 429:
            break
        retry_after = float(resp.headers.get("Retry-After", 2 * attempt))
        print(f"Groq rate-limited (attempt {attempt}/{max_attempts}), "
              f"waiting {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)

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
        "years_experience": str(parsed.get("years_experience", "Not mentioned")).strip(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_HARD_GERMAN_LEVEL_WORDS = ("c1", "c2", "fluent", "native")


def is_hard_german_requirement(german_requirement: str) -> bool:
    """
    True if the LLM-extracted German requirement is a MANDATORY C1+,
    fluent, or native-level requirement. Per our prompt schema, the
    'plus, not required' case contains the phrase 'not required', so we
    explicitly exclude that from matching — otherwise a naive substring
    check on "required" would incorrectly flag optional mentions too.
    """
    text = (german_requirement or "").lower()
    if "not required" in text:
        return False
    if "required" not in text:
        return False
    return any(word in text for word in _HARD_GERMAN_LEVEL_WORDS)


def run() -> None:
    state = load_state(STATE_FILE)
    print(f"DEBUG loaded state from {STATE_FILE}: {state}")
    last_seen = parse_iso(state["last_seen_iso"])
    recent_ids = set(state["recent_ids"])

    # Fetch both scopes and merge into one deduped list, keyed by job id —
    # this is what makes a Berlin job (which matches both queries) get
    # processed exactly once instead of twice.
    all_jobs: dict[str, dict] = {}
    for location in FETCH_LOCATIONS:
        for job in fetch_jobs(location):
            job_id = str(job.get("id"))
            if job_id:
                all_jobs.setdefault(job_id, job)
    jobs = list(all_jobs.values())
    print(f"DEBUG merged {len(jobs)} unique jobs across both scopes")

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
            # Clamp to "now" — job boards occasionally report bogus future
            # 'created' timestamps (reposts/bumps, clock mismatches on the
            # source's end). If we let that poison the cursor, a genuinely
            # new job posted between now and that fake future point would
            # look "older than the cursor" next run and be silently
            # skipped forever. Capping at now prevents that.
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if created > newest_iso and created <= now_iso:
                newest_iso = created
            elif created > now_iso:
                print(f"WARNING '{job.get('title')}' has a future-dated "
                      f"created timestamp ({created}) — not advancing "
                      f"cursor past it")

            exclude_reason = should_exclude(job)
            if exclude_reason:
                print(f"Skipped ({exclude_reason}): {job.get('title')}")
                continue

            new_jobs.append(job)

    # Pass 1: enrich, score, and filter — build the final list of jobs
    # that will actually be sent, so the batch header count is accurate
    # (rather than counting jobs that get filtered out by the German
    # requirement check below).
    to_send = []
    for job in new_jobs:
        label = label_for_job(job)
        description = enrich_description(job)
        print(f"DEBUG scoring '{job.get('title')}' with description "
              f"length {len(description)} chars")

        try:
            match = score_job_match(job, description)
        except Exception as exc:  # noqa: BLE001
            # A scoring failure should never block the job from being sent
            # or block state persistence — fall back to no score shown.
            print(f"WARNING scoring failed for '{job.get('title')}': {exc}",
                  file=sys.stderr)
            match = {"match_score": None, "match_reason": "",
                     "german_requirement": "Unknown", "years_experience": "Not mentioned"}

        if is_hard_german_requirement(match.get("german_requirement", "")):
            print(f"Skipped (requires {match.get('german_requirement')}): "
                  f"{job.get('title')}")
            continue

        to_send.append((job, label, match))
        time.sleep(1.0)  # be polite to Groq's rate limits (larger model = tighter limits)

    # Pass 2: send the batch header (now with an accurate count), then
    # each job.
    if to_send:
        try:
            send_batch_header(len(to_send))
        except Exception as exc:  # noqa: BLE001
            # Never let the divider message block the actual job alerts.
            print(f"WARNING failed to send batch header: {exc}", file=sys.stderr)

    for job, label, match in to_send:
        try:
            send_telegram(label, job, match)
            print(f"Sent alert ({label}): {job.get('title')} "
                  f"(match={match.get('match_score')})")
        except Exception as exc:  # noqa: BLE001
            # Critical: never let one bad message crash the whole run —
            # that would skip save_state() below and reset the cursor to
            # square one, causing every job to be resent next run. Log and
            # move on; the cursor still advances since recent_ids/newest_iso
            # were already updated in the filtering loop above.
            print(f"ERROR sending Telegram message for "
                  f"'{job.get('title')}': {exc}", file=sys.stderr)
        time.sleep(0.3)  # be polite to Telegram's rate limits

    # Trim recent_ids to the cap (keep most recently added — since sets
    # don't preserve order, just cap by re-deriving from the current jobs
    # payload order, newest first).
    ordered_recent = [str(j["id"]) for j in jobs if str(j.get("id")) in recent_ids]
    trimmed = ordered_recent[:RECENT_ID_CAP] if ordered_recent else list(recent_ids)[:RECENT_ID_CAP]

    new_state = {
        "last_seen_iso": newest_iso,
        "recent_ids": trimmed,
    }
    print(f"DEBUG about to write state: {new_state}")
    save_state(STATE_FILE, new_state)
    print(f"DEBUG state file now on disk: {STATE_FILE.read_text()}")

    if not new_jobs:
        print("No new jobs this run.")


def main() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
