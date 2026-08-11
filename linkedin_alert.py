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

# Config
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

CANDIDATE_PROFILE = os.environ["CANDIDATE_PROFILE"]

CONFIG_FILE = Path(__file__).parent / "linkedin_search_config.txt"

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

# LinkedIn's guest endpoint is unauthenticated and its ToS prohibits automated
# access - ran from a residential IP this stayed under the radar at hourly
# cadence; from a GitHub Actions runner (a known datacenter IP range) the same
# pattern is more likely to get flagged. Keep this User-Agent, the small
# per-request delay below, and a conservative external cron schedule (see
# README) - this is personal-use, low-volume by design, not a bulk scraper.
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; linkedin-alert-bot/1.0; personal use)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

_DEFAULT_KEYWORDS = [
    "data scientist", "data science", "machine learning engineer",
    "ML engineer", "applied scientist", "research scientist", "data analyst",
]
_DEFAULT_EXCLUDE_TITLES = [
    "praktikum", "praktikant", "werkstudent", "intern", "internship",
    "working student", "duales studium", "dual study", "software engineer",
    "data engineer",
]
_DEFAULT_EXCLUDE_GERMAN_LEVELS = ["c1", "c2", "fluent", "native"]
_DEFAULT_LOCATIONS = ["Berlin, Germany", "Germany"]
_DEFAULT_MIN_MATCH_SCORE = 4
_DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
_DEFAULT_GROQ_CALL_PACING_SECONDS = 8
_DEFAULT_MAX_JOBS_SCORED_PER_RUN = 60
_DEFAULT_MAX_DESCRIPTION_CHARS = 2000


def load_search_config(path: Path) -> dict:
    """
    Same plain-text format as search_config.txt (see that file / job_alert.py
    for the full format description). No ADZUNA_COUNTRY section here - not
    applicable to LinkedIn - and LOCATIONS are passed to LinkedIn's location
    param as literal text (no "Germany" -> nationwide-None translation;
    LinkedIn's endpoint just takes "Germany" directly).
    """
    result = {
        "KEYWORDS": [],
        "EXCLUDE_TITLES": [],
        "EXCLUDE_GERMAN_LEVELS": [],
        "LOCATIONS": [],
        "MIN_MATCH_SCORE": [],
        "GROQ_MODEL": [],
        "GROQ_CALL_PACING_SECONDS": [],
        "MAX_JOBS_SCORED_PER_RUN": [],
        "MAX_DESCRIPTION_CHARS": [],
    }

    if not path.exists():
        print(f"WARNING {path} not found — using built-in defaults", file=sys.stderr)
    else:
        current_section = None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                current_section = section if section in result else None
                if current_section is None:
                    print(f"WARNING unknown section '{section}' in "
                          f"{path.name} — ignoring", file=sys.stderr)
                continue
            if current_section:
                result[current_section].append(line)

    keywords = result["KEYWORDS"] or _DEFAULT_KEYWORDS
    exclude_titles = result["EXCLUDE_TITLES"] or _DEFAULT_EXCLUDE_TITLES
    exclude_german_levels = [w.lower() for w in result["EXCLUDE_GERMAN_LEVELS"]] or _DEFAULT_EXCLUDE_GERMAN_LEVELS
    locations = result["LOCATIONS"] or _DEFAULT_LOCATIONS

    def _single_str(key: str, default: str) -> str:
        return result[key][0].strip() if result[key] else default

    def _single_int(key: str, default: int) -> int:
        try:
            return int(result[key][0])
        except (IndexError, ValueError):
            if result[key]:
                print(f"WARNING invalid value for [{key}] in {path.name} "
                      f"— using default {default}", file=sys.stderr)
            return default

    min_match_score = _single_int("MIN_MATCH_SCORE", _DEFAULT_MIN_MATCH_SCORE)
    groq_model = _single_str("GROQ_MODEL", _DEFAULT_GROQ_MODEL)
    groq_call_pacing_seconds = _single_int("GROQ_CALL_PACING_SECONDS", _DEFAULT_GROQ_CALL_PACING_SECONDS)
    max_jobs_scored_per_run = _single_int("MAX_JOBS_SCORED_PER_RUN", _DEFAULT_MAX_JOBS_SCORED_PER_RUN)
    max_description_chars = _single_int("MAX_DESCRIPTION_CHARS", _DEFAULT_MAX_DESCRIPTION_CHARS)

    return {
        "KEYWORDS": keywords,
        "EXCLUDE_TITLES": exclude_titles,
        "EXCLUDE_GERMAN_LEVELS": exclude_german_levels,
        "LOCATIONS": locations,
        "MIN_MATCH_SCORE": min_match_score,
        "GROQ_MODEL": groq_model,
        "GROQ_CALL_PACING_SECONDS": groq_call_pacing_seconds,
        "MAX_JOBS_SCORED_PER_RUN": max_jobs_scored_per_run,
        "MAX_DESCRIPTION_CHARS": max_description_chars,
    }


_CONFIG = load_search_config(CONFIG_FILE)
print(f"DEBUG loaded search config: keywords={_CONFIG['KEYWORDS']}, "
      f"exclude_titles={_CONFIG['EXCLUDE_TITLES']}, "
      f"exclude_german_levels={_CONFIG['EXCLUDE_GERMAN_LEVELS']}, "
      f"locations={_CONFIG['LOCATIONS']}, "
      f"min_match_score={_CONFIG['MIN_MATCH_SCORE']}, "
      f"groq_model={_CONFIG['GROQ_MODEL']}, "
      f"groq_call_pacing_seconds={_CONFIG['GROQ_CALL_PACING_SECONDS']}, "
      f"max_jobs_scored_per_run={_CONFIG['MAX_JOBS_SCORED_PER_RUN']}, "
      f"max_description_chars={_CONFIG['MAX_DESCRIPTION_CHARS']}")

KEYWORDS = _CONFIG["KEYWORDS"]
LOCATIONS = _CONFIG["LOCATIONS"]
MIN_MATCH_SCORE = _CONFIG["MIN_MATCH_SCORE"]
GROQ_MODEL = _CONFIG["GROQ_MODEL"]
GROQ_CALL_PACING_SECONDS = _CONFIG["GROQ_CALL_PACING_SECONDS"]
MAX_JOBS_SCORED_PER_RUN = _CONFIG["MAX_JOBS_SCORED_PER_RUN"]
MAX_DESCRIPTION_CHARS = _CONFIG["MAX_DESCRIPTION_CHARS"]

EXCLUDE_TITLE_PATTERNS = [rf"\b{re.escape(phrase)}\b" for phrase in _CONFIG["EXCLUDE_TITLES"]]
_EXCLUDE_TITLE_RE = re.compile("|".join(EXCLUDE_TITLE_PATTERNS), re.IGNORECASE)

# How many recent IDs to remember, as a tie-break safety net
RECENT_ID_CAP = 100

STATE_DIR = Path(__file__).parent / "state"
STATE_FILE = STATE_DIR / "linkedin.json"


def should_exclude(title: str) -> str | None:
    if _EXCLUDE_TITLE_RE.search(title):
        return "internship/working-student role"
    return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"last_seen_iso": "1970-01-01T00:00:00Z", "recent_ids": []}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# LinkedIn search + detail fetch (public "jobs-guest" endpoint, no auth)
# ---------------------------------------------------------------------------

def http_get_with_retry(url: str, max_retries: int = 3) -> requests.Response | None:
    """GET with a short retry/backoff on 429/5xx. Returns None (not raises) on
    a 404 or after exhausting retries - a fetch failure for one query should
    never crash the whole run."""
    delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        except requests.RequestException as exc:
            if attempt == max_retries:
                print(f"WARNING request failed after retries: {url} ({exc})", file=sys.stderr)
                return None
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                print(f"WARNING giving up after {max_retries} retries: "
                      f"HTTP {resp.status_code} for {url}", file=sys.stderr)
                return None
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
            continue
        return resp
    return None


def build_search_url(query: str, location: str) -> str:
    params = {
        "keywords": query,
        "location": location,
        "start": "0",
        # Most-recent-first (LinkedIn's default is relevance-ranked). With
        # only page 1 (10 results) fetched per combo and no time filter, this
        # matters: a stale-but-relevant posting could otherwise sit above a
        # genuinely new one.
        "sortBy": "DD",
    }
    return f"{SEARCH_URL}?{requests.compat.urlencode(params)}"


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", fragment)).strip()


def _clean(fragment: str) -> str:
    return html.unescape(_strip_tags(fragment))


def parse_job_cards(page_html: str) -> list[dict]:
    results = []
    chunks = page_html.split('data-entity-urn="urn:li:jobPosting:')[1:]
    for chunk in chunks:
        id_match = re.match(r"^(\d+)", chunk)
        if not id_match:
            continue
        job_id = id_match.group(1)

        link_match = re.search(
            r'class="base-card__full-link[^"]*"[^>]*href="([^"]+)"', chunk, re.IGNORECASE
        )
        url = html.unescape(link_match.group(1)).split("?")[0] if link_match else ""

        title = None
        h3 = re.search(r'class="base-search-card__title"[^>]*>(.*?)</h3>', chunk, re.IGNORECASE | re.DOTALL)
        if h3:
            title = _clean(h3.group(1))
        if not title:
            sr = re.search(r'class="sr-only"[^>]*>(.*?)</span>', chunk, re.IGNORECASE | re.DOTALL)
            if sr:
                title = _clean(sr.group(1))
        if not title:
            continue

        company = None
        sub = re.search(
            r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', chunk, re.IGNORECASE | re.DOTALL
        )
        if sub:
            company = _clean(sub.group(1)) or None

        loc = re.search(r'class="job-search-card__location"[^>]*>(.*?)</span>', chunk, re.IGNORECASE | re.DOTALL)
        location = _clean(loc.group(1)) if loc else None
        if location == "":
            location = None

        dt = re.search(
            r'class="job-search-card__listdate[^"]*"[^>]*datetime="([^"]+)"', chunk, re.IGNORECASE
        )
        # LinkedIn's search cards only expose a calendar date here, never a
        # time of day - do not treat this as more precise than it is.
        date = dt.group(1) if dt else None

        results.append(
            {
                "id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "date": date,
                "url": url or f"https://www.linkedin.com/jobs/view/{job_id}",
            }
        )
    return results


def fetch_jobs() -> list[dict]:
    """Query each keyword/location combo (page 1 only, most-recent-first),
    merge and dedupe by job ID."""
    seen_ids = set()
    merged: list[dict] = []
    for location in LOCATIONS:
        for keyword in KEYWORDS:
            resp = http_get_with_retry(build_search_url(keyword, location))
            if resp is None:
                continue
            for card in parse_job_cards(resp.text):
                if card["id"] not in seen_ids:
                    seen_ids.add(card["id"])
                    merged.append(card)
            time.sleep(1.5)  # be polite - see the ToS note at the top of this file
    return merged


def extract_div_content(page_html: str, class_name: str) -> str | None:
    """Extract inner HTML of a <div class="...class_name...">, tracking
    nested divs by depth (LinkedIn's description markup nests divs inside
    the description block, so a naive non-greedy regex would truncate early)."""
    escaped = re.escape(class_name)
    open_re = re.compile(rf'<div[^>]*class="[^"]*{escaped}[^"]*"[^>]*>', re.IGNORECASE)
    m = open_re.search(page_html)
    if not m:
        return None
    i = m.end()
    depth = 1
    n = len(page_html)
    while depth > 0 and i < n:
        next_open = page_html.find("<div", i)
        next_close = page_html.find("</div>", i)
        if next_close == -1:
            return None
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + 4
        else:
            depth -= 1
            i = next_close + 6
    return page_html[m.end():i - 6]


def fetch_job_full_text(job_id: str) -> str:
    """Fetch the LinkedIn detail page and return its cleaned full text (used
    both as the description window source and as the corpus scanned for
    German-language mentions). Returns "" on any failure - never raises."""
    resp = http_get_with_retry(f"{DETAIL_URL}/{job_id}")
    if resp is None:
        return ""
    desc_html = extract_div_content(resp.text, "show-more-less-html__markup") or extract_div_content(
        resp.text, "description__text"
    )
    if not desc_html:
        return ""
    with_breaks = re.sub(r"<\s*br\s*/?>", "\n", desc_html, flags=re.IGNORECASE)
    with_breaks = re.sub(r"</(p|li|ul|ol|div|h\d)>", "\n", with_breaks, flags=re.IGNORECASE)
    text = html.unescape(_strip_tags(with_breaks))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# German-mention extraction (pure regex, zero LLM token cost) and description
# windowing - ported as-is from job_alert.py for consistency.
# ---------------------------------------------------------------------------

_REQUIREMENT_SECTION_MARKERS_RE = re.compile(
    r"\b(your profile|who you are|requirements|qualifications|what you bring|"
    r"must have|we\S{0,3} looking for|dein profil|ihr profil|anforderungen|"
    r"was du mitbringst|was sie mitbringen|voraussetzungen|deine qualifikation|"
    r"sprachkenntnisse)\b",
    re.IGNORECASE,
)


def _extract_relevant_window(text: str, total_budget: int) -> str:
    """Build a text window within `total_budget` chars, prioritizing the
    requirements/qualifications section if one is found anywhere in the text
    (language requirements most commonly live there) - falls back to a
    head+tail split otherwise, which has better odds of catching a
    requirements section near the end than a single continuous block from
    the start."""
    if len(text) <= total_budget:
        return text

    marker_match = _REQUIREMENT_SECTION_MARKERS_RE.search(text)
    if marker_match:
        req_idx = marker_match.start()
        context_budget = total_budget // 3
        req_budget = total_budget - context_budget
        context = text[:context_budget]
        requirements = text[req_idx:req_idx + req_budget]
        return f"{context}\n...\n{requirements}"

    head_budget = total_budget // 2
    tail_budget = total_budget - head_budget
    head = text[:head_budget]
    tail = text[-tail_budget:]
    return f"{head}\n...\n{tail}"


_GERMAN_MENTION_RE = re.compile(r"\b(german|deutsch\w*)\b", re.IGNORECASE)


def extract_german_mentions(text: str, context_chars: int = 150, max_mentions: int = 5) -> str:
    if not text:
        return ""
    mentions = []
    seen_spans = set()
    for match in _GERMAN_MENTION_RE.finditer(text):
        start = max(0, match.start() - context_chars)
        end = min(len(text), match.end() + context_chars)
        span_key = (start // 50, end // 50)
        if span_key in seen_spans:
            continue
        seen_spans.add(span_key)
        mentions.append(text[start:end].strip())
        if len(mentions) >= max_mentions:
            break
    return "\n---\n".join(mentions)


def enrich_description(job: dict) -> dict:
    """Return {"description": <windowed text for scoring>, "german_context":
    <focused German-mention excerpts, or "" if none found>}. Never raises -
    falls back to empty strings on any fetch failure."""
    full_text = fetch_job_full_text(job["id"])
    description = _extract_relevant_window(full_text, MAX_DESCRIPTION_CHARS) if full_text else ""
    german_context = extract_german_mentions(full_text)
    return {"description": description, "german_context": german_context}


# ---------------------------------------------------------------------------
# Groq scoring - same prompt schema and retry strategy as job_alert.py
# ---------------------------------------------------------------------------

def score_job_match(job: dict, description: str, german_context: str) -> dict:
    title = job.get("title", "")

    german_section = (
        f"Excerpts from the posting that mention German:\n{german_context}"
        if german_context
        else "No mention of 'German' or 'Deutsch' was found anywhere in "
             "the full posting text."
    )

    system_prompt = (
        "You are a job-matching assistant. Given a candidate profile, a "
        "job posting, and separately-extracted excerpts about German "
        "language mentions, respond with STRICT JSON only — no markdown, "
        "no code fences, no extra text — in exactly this schema:\n"
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
        "For german_requirement: base your answer ONLY on the separately "
        "provided German-mention excerpts (not the main posting text — "
        "those excerpts are the authoritative, complete set of every "
        "German mention found anywhere in the full posting). Use exactly "
        "one of these forms:\n"
        "- 'Not mentioned' — if told no mention was found.\n"
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
        f"JOB POSTING:\nTitle: {title}\nDescription: {description}\n\n"
        f"{german_section}"
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

    max_attempts = 3
    max_retry_wait_seconds = 15
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
        if retry_after > max_retry_wait_seconds:
            print(f"Groq rate-limited, suggested wait {retry_after}s exceeds "
                  f"cap of {max_retry_wait_seconds}s — giving up on this "
                  f"job's score rather than waiting", file=sys.stderr)
            break
        print(f"Groq rate-limited (attempt {attempt}/{max_attempts}), "
              f"waiting {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)

    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]

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


_HARD_GERMAN_LEVEL_WORDS = tuple(_CONFIG["EXCLUDE_GERMAN_LEVELS"])


def is_hard_german_requirement(german_requirement: str) -> bool:
    text = (german_requirement or "").lower()
    if "not required" in text:
        return False
    if "required" not in text:
        return False
    return any(word in text for word in _HARD_GERMAN_LEVEL_WORDS)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def format_posted_date(date_str: str | None) -> str:
    """LinkedIn's search cards only expose a calendar date, never a time of
    day - deliberately date-only, do not fabricate a time component."""
    if not date_str:
        return "Unknown"
    try:
        dt = parse_iso(date_str)
        return dt.strftime("%d %b %Y")
    except ValueError:
        return date_str


def send_batch_header(count: int) -> None:
    now_berlin = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d %b %Y, %I:%M %p")
    text = (
        f"💼 NEW BATCH (LinkedIn) — {count} ROLE{'S' if count != 1 else ''} 💼\n"
        f"{now_berlin}"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(api_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()


def send_telegram(job: dict, match: dict) -> None:
    title = job.get("title", "Untitled role")
    company = job.get("company") or "Unknown company"
    location = job.get("location") or "Unknown location"
    url = job.get("url", "")

    posted_line = f"\n\n🕒 Posted: {html.escape(format_posted_date(job.get('date')))}"

    score = match.get("match_score")
    score_line = ""
    if score:
        score_line = f"\n\n🎯 Match: {html.escape(str(score))}/10 — {html.escape(match.get('match_reason', ''))}"

    german_line = f"\n\n🇩🇪 German: {html.escape(match.get('german_requirement', 'Not mentioned'))}"

    exp = match.get("years_experience", "Not mentioned")
    exp_line = ""
    if exp and exp.lower() != "not mentioned":
        exp_line = f"\n\n📅 Experience: {html.escape(exp)}"

    text = (
        f"🔗 LinkedIn — New role\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{html.escape(company)} · {html.escape(location)}{posted_line}"
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    state = load_state(STATE_FILE)
    print(f"DEBUG loaded state from {STATE_FILE}: {state}")
    recent_ids = set(state["recent_ids"])
    is_first_run = not recent_ids and state.get("last_seen_iso") == "1970-01-01T00:00:00Z"

    jobs = fetch_jobs()
    print(f"DEBUG fetched {len(jobs)} unique job cards across "
          f"{len(LOCATIONS)} location(s) x {len(KEYWORDS)} keyword(s)")

    if is_first_run:
        # No history yet - unlike job_alert.py's Adzuna cursor (where the
        # README tells you to manually pre-set last_seen_iso to avoid a
        # first-run flood), just seed every currently-open matching posting
        # as "already known" and send one confirmation instead. No scoring
        # needed for a baseline run.
        all_ids = [j["id"] for j in jobs]
        trimmed = all_ids[-RECENT_ID_CAP:] if len(all_ids) > RECENT_ID_CAP else all_ids
        save_state(STATE_FILE, {
            "last_seen_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recent_ids": trimmed,
        })
        try:
            api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(
                api_url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": f"✅ LinkedIn job alert initialized.\nTracking {len(jobs)} existing "
                    f"posting(s) as baseline. You'll get a message here whenever a new "
                    f"matching posting appears.",
                },
                timeout=15,
            ).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING failed to send first-run confirmation: {exc}", file=sys.stderr)
        print(f"First run: seeded {len(jobs)} jobs as baseline, no notifications sent.")
        return

    new_jobs = [j for j in jobs if j["id"] not in recent_ids]

    to_send = []
    for i, job in enumerate(new_jobs):
        recent_ids.add(job["id"])

        exclude_reason = should_exclude(job["title"])
        if exclude_reason:
            print(f"Skipped ({exclude_reason}): {job['title']}")
            continue

        if i >= MAX_JOBS_SCORED_PER_RUN:
            print(f"Skipping score for '{job['title']}' — per-run scoring "
                  f"cap ({MAX_JOBS_SCORED_PER_RUN}) reached")
            match = {"match_score": None, "match_reason": "",
                     "german_requirement": "Unknown", "years_experience": "Not mentioned"}
            to_send.append((job, match))
            continue

        enriched = enrich_description(job)
        description = enriched["description"]
        german_context = enriched["german_context"]
        print(f"DEBUG scoring '{job['title']}' with description length "
              f"{len(description)} chars, german_context "
              f"{'found (' + str(len(german_context)) + ' chars)' if german_context else 'NOT found'}")

        try:
            match = score_job_match(job, description, german_context)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING scoring failed for '{job['title']}': {exc}", file=sys.stderr)
            match = {"match_score": None, "match_reason": "",
                     "german_requirement": "Unknown", "years_experience": "Not mentioned"}

        if is_hard_german_requirement(match.get("german_requirement", "")):
            print(f"Skipped (requires {match.get('german_requirement')}): {job['title']}")
            continue

        score = match.get("match_score")
        if score is not None and score < MIN_MATCH_SCORE:
            print(f"Skipped (match score {score} < {MIN_MATCH_SCORE}): {job['title']}")
            continue

        to_send.append((job, match))
        time.sleep(GROQ_CALL_PACING_SECONDS)

    if to_send:
        try:
            send_batch_header(len(to_send))
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING failed to send batch header: {exc}", file=sys.stderr)

    for job, match in to_send:
        try:
            send_telegram(job, match)
            print(f"Sent alert: {job['title']} (match={match.get('match_score')})")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR sending Telegram message for '{job['title']}': {exc}", file=sys.stderr)
        time.sleep(0.3)  # be polite to Telegram's rate limits

    trimmed = list(recent_ids)[-RECENT_ID_CAP:] if len(recent_ids) > RECENT_ID_CAP else list(recent_ids)
    new_state = {
        "last_seen_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
