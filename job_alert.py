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

#Config
ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

CANDIDATE_PROFILE = os.environ["CANDIDATE_PROFILE"]

CONFIG_FILE = Path(__file__).parent / "search_config.txt"

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
_DEFAULT_LOCATIONS = ["Germany", "Berlin"]
_DEFAULT_MIN_MATCH_SCORE = 4
_DEFAULT_ADZUNA_COUNTRY = "de"
_DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
_DEFAULT_GROQ_CALL_PACING_SECONDS = 8
_DEFAULT_MAX_JOBS_SCORED_PER_RUN = 60
_DEFAULT_MAX_DESCRIPTION_CHARS = 2000


def load_search_config(path: Path) -> dict:
    """
    Parse the plain-text search config file into a dict of settings.
    Format: '[SECTION]' headers followed by one value per line; '#'
    starts a comment; blank lines are ignored. Missing file or missing/
    empty sections fall back to the defaults above, with a warning —
    this should never crash the run over a config typo.
    """
    result = {
        "KEYWORDS": [],
        "EXCLUDE_TITLES": [],
        "EXCLUDE_GERMAN_LEVELS": [],
        "LOCATIONS": [],
        "MIN_MATCH_SCORE": [],
        "ADZUNA_COUNTRY": [],
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
    adzuna_country = _single_str("ADZUNA_COUNTRY", _DEFAULT_ADZUNA_COUNTRY).lower()
    groq_model = _single_str("GROQ_MODEL", _DEFAULT_GROQ_MODEL)
    groq_call_pacing_seconds = _single_int("GROQ_CALL_PACING_SECONDS", _DEFAULT_GROQ_CALL_PACING_SECONDS)
    max_jobs_scored_per_run = _single_int("MAX_JOBS_SCORED_PER_RUN", _DEFAULT_MAX_JOBS_SCORED_PER_RUN)
    max_description_chars = _single_int("MAX_DESCRIPTION_CHARS", _DEFAULT_MAX_DESCRIPTION_CHARS)


    fetch_locations = [None if loc.strip().lower() == "germany" else loc for loc in locations]

    return {
        "KEYWORDS": keywords,
        "EXCLUDE_TITLES": exclude_titles,
        "EXCLUDE_GERMAN_LEVELS": exclude_german_levels,
        "FETCH_LOCATIONS": fetch_locations,
        "MIN_MATCH_SCORE": min_match_score,
        "ADZUNA_COUNTRY": adzuna_country,
        "GROQ_MODEL": groq_model,
        "GROQ_CALL_PACING_SECONDS": groq_call_pacing_seconds,
        "MAX_JOBS_SCORED_PER_RUN": max_jobs_scored_per_run,
        "MAX_DESCRIPTION_CHARS": max_description_chars,
    }


_CONFIG = load_search_config(CONFIG_FILE)
print(f"DEBUG loaded search config: keywords={_CONFIG['KEYWORDS']}, "
      f"exclude_titles={_CONFIG['EXCLUDE_TITLES']}, "
      f"exclude_german_levels={_CONFIG['EXCLUDE_GERMAN_LEVELS']}, "
      f"locations={_CONFIG['FETCH_LOCATIONS']}, "
      f"min_match_score={_CONFIG['MIN_MATCH_SCORE']}, "
      f"adzuna_country={_CONFIG['ADZUNA_COUNTRY']}, "
      f"groq_model={_CONFIG['GROQ_MODEL']}, "
      f"groq_call_pacing_seconds={_CONFIG['GROQ_CALL_PACING_SECONDS']}, "
      f"max_jobs_scored_per_run={_CONFIG['MAX_JOBS_SCORED_PER_RUN']}, "
      f"max_description_chars={_CONFIG['MAX_DESCRIPTION_CHARS']}")

ADZUNA_COUNTRY = _CONFIG["ADZUNA_COUNTRY"]
KEYWORDS = _CONFIG["KEYWORDS"]


MIN_MATCH_SCORE = _CONFIG["MIN_MATCH_SCORE"]


GROQ_MODEL = _CONFIG["GROQ_MODEL"]


GROQ_CALL_PACING_SECONDS = _CONFIG["GROQ_CALL_PACING_SECONDS"]
MAX_JOBS_SCORED_PER_RUN = _CONFIG["MAX_JOBS_SCORED_PER_RUN"]
MAX_DESCRIPTION_CHARS = _CONFIG["MAX_DESCRIPTION_CHARS"]


EXCLUDE_TITLE_PATTERNS = [rf"\b{re.escape(phrase)}\b" for phrase in _CONFIG["EXCLUDE_TITLES"]]
_EXCLUDE_TITLE_RE = re.compile("|".join(EXCLUDE_TITLE_PATTERNS), re.IGNORECASE)

# How many recent IDs to remember, as a tie-break safety net
RECENT_ID_CAP = 100

# Results per page to pull each run (Adzuna max is 50)
RESULTS_PER_PAGE = 50

STATE_DIR = Path(__file__).parent / "state"
STATE_FILE = STATE_DIR / "combined.json"

FETCH_LOCATIONS = _CONFIG["FETCH_LOCATIONS"]


def label_for_job(job: dict) -> str:
    """Pick a display label based on the job's actual location, checking
    against whichever specific (non-nationwide) locations are configured."""
    display_name = job.get("location", {}).get("display_name", "").lower()
    for loc in FETCH_LOCATIONS:
        if loc and loc.lower() in display_name:
            return f"📍 {loc}"
    return "🇩🇪 Germany"


def should_exclude(job: dict) -> str | None:
    """Return a short reason string if the job should be skipped, else None."""
    title = job.get("title", "")

    if _EXCLUDE_TITLE_RE.search(title):
        return "internship/working-student role"

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


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _clean_html_fragment(raw: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def extract_jsonld_job_description(page_html: str) -> str | None:
    """
    Many job boards — including JS-rendered ones like StepStone, where the
    visible page requires JavaScript to display content — still embed a
    schema.org JobPosting JSON-LD block server-side, purely for Google's
    Job Search SEO. When present, it's a much more reliable source than
    scraping visible text, since it exists regardless of client-side
    rendering. Returns None if no JobPosting block is found/parseable.
    """
    for match in _JSONLD_RE.finditer(page_html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            # Some sites nest postings under an @graph array
            graph = item.get("@graph")
            sub_candidates = graph if isinstance(graph, list) else [item]
            for sub in sub_candidates:
                if not isinstance(sub, dict):
                    continue
                if sub.get("@type") == "JobPosting" and sub.get("description"):
                    # The description field itself commonly contains raw
                    # HTML (e.g. "<p>...</p><ul><li>...") — clean it too.
                    cleaned = _clean_html_fragment(str(sub["description"]))
                    if cleaned:
                        return cleaned
    return None


_REQUIREMENT_SECTION_MARKERS_RE = re.compile(
    r"\b(your profile|who you are|requirements|qualifications|what you bring|"
    r"must have|we\S{0,3} looking for|dein profil|ihr profil|anforderungen|"
    r"was du mitbringst|was sie mitbringen|voraussetzungen|deine qualifikation|"
    r"sprachkenntnisse)\b",
    re.IGNORECASE,
)


def _extract_relevant_window(text: str, anchor_idx: int, total_budget: int) -> str:
    """
    Build a text window within `total_budget` chars, prioritizing:
    1. Some initial context from the known job content (anchor_idx, or
       start of page if no anchor was found).
    2. The requirements/qualifications section specifically, if a marker
       for one is found anywhere in the text — language requirements
       most commonly live there, and a fixed continuous block from a
       single point can miss it entirely if the section falls outside
       that block on a long posting.
    Falls back to a head+tail split of the whole page if no requirements
    marker is found — better odds of catching content near the end
    (where requirements sections often are, even without a header we
    recognize) than a single continuous block from the start.
    """
    start = max(anchor_idx, 0)

    if len(text) - start <= total_budget:
        return text[start:start + total_budget]

    marker_match = _REQUIREMENT_SECTION_MARKERS_RE.search(text, start)
    if marker_match:
        req_idx = marker_match.start()
        context_budget = total_budget // 3
        req_budget = total_budget - context_budget
        context = text[start:start + context_budget]
        requirements = text[req_idx:req_idx + req_budget]
        return f"{context}\n...\n{requirements}"

    head_budget = total_budget // 2
    tail_budget = total_budget - head_budget
    head = text[start:start + head_budget]
    tail = text[-tail_budget:]
    return f"{head}\n...\n{tail}"


_GERMAN_MENTION_RE = re.compile(r"\b(german|deutsch\w*)\b", re.IGNORECASE)


def extract_german_mentions(text: str, context_chars: int = 150, max_mentions: int = 5) -> str:
    """
    Scan text for any mention of 'German'/'Deutsch' and return the
    surrounding context for each occurrence, deduplicated. This is plain
    local regex scanning — zero LLM token cost — so it's run on the
    FULL fetched page text, not the token-budget-limited description
    window. That means a language requirement can never be missed due
    to truncation, regardless of where in a long posting it appears.
    Only the small extracted excerpts (not the whole page) get sent to
    Groq afterward, for a much cheaper and more reliable check than
    asking the model to find it within the whole JD itself.
    """
    if not text:
        return ""
    mentions = []
    seen_spans = set()
    for match in _GERMAN_MENTION_RE.finditer(text):
        start = max(0, match.start() - context_chars)
        end = min(len(text), match.end() + context_chars)
        span_key = (start // 50, end // 50)  # coarse dedupe of overlapping windows
        if span_key in seen_spans:
            continue
        seen_spans.add(span_key)
        mentions.append(text[start:end].strip())
        if len(mentions) >= max_mentions:
            break
    return "\n---\n".join(mentions)


def enrich_description(job: dict) -> dict:
    """
    Return {"description": <windowed text for match-score/years-exp>,
             "german_context": <focused German-mention excerpts, or ""
             if none found anywhere on the full page>}.

    Adzuna's API 'description' field is frequently a truncated snippet —
    even when it LOOKS reasonably long, it can still cut off before
    reaching requirements near the bottom of a posting. So we always
    attempt to fetch the full posting page. The 'description' window is
    still capped at MAX_DESCRIPTION_CHARS (for match-scoring token cost),
    but 'german_context' is extracted from the FULL page separately — see
    extract_german_mentions — decoupling the German check entirely from
    that token budget.

    Falls back to the original snippet / empty german_context on any
    failure — this must never raise, since a fetch failure shouldn't
    block scoring or sending the job.
    """
    description = job.get("description", "") or ""
    url = job.get("redirect_url", "")

    full_text_for_scan = description
    windowed_description = description

    if not url:
        return {"description": windowed_description,
                "german_context": extract_german_mentions(full_text_for_scan)}

    if "linkedin.com" in url.lower():

        print(f"DEBUG '{job.get('title')}' redirects to LinkedIn — "
              f"fetch may be blocked or return a login-walled preview")

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobAlertBot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()


        jsonld_desc = extract_jsonld_job_description(resp.text)
        if jsonld_desc and len(jsonld_desc) > len(description):
            full_text_for_scan = jsonld_desc
            windowed_description = _extract_relevant_window(jsonld_desc, 0, MAX_DESCRIPTION_CHARS)
            print(f"DEBUG found JSON-LD JobPosting description for "
                  f"'{job.get('title')}' ({len(windowed_description)} chars "
                  f"used for scoring, {len(full_text_for_scan)} chars scanned "
                  f"for German mentions)")
        else:
            # Strategy 2: anchor-based text extraction from the raw page.
            text = _clean_html_fragment(resp.text)
            full_text_for_scan = text if len(text) > len(description) else description


            anchor = description[50:150].strip() if len(description) > 150 else description.strip()
            idx = text.find(anchor) if anchor else -1

            candidate = _extract_relevant_window(text, idx, MAX_DESCRIPTION_CHARS)
            if idx != -1:
                print(f"DEBUG anchor found for '{job.get('title')}' at index "
                      f"{idx}; extracted window prioritizing requirements "
                      f"section ({len(candidate)} chars for scoring, "
                      f"{len(full_text_for_scan)} chars scanned for German)")
            else:
                print(f"DEBUG anchor NOT found for '{job.get('title')}'; "
                      f"extracted window from start of page ({len(candidate)} "
                      f"chars for scoring, {len(full_text_for_scan)} chars "
                      f"scanned for German)")

            if len(candidate) > len(description):
                windowed_description = candidate
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING full-JD fetch failed for '{job.get('title')}': {exc}",
              file=sys.stderr)

    german_context = extract_german_mentions(full_text_for_scan)
    return {"description": windowed_description, "german_context": german_context}


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
        f"💼 NEW BATCH — {count} ROLE{'S' if count != 1 else ''} 💼\n"
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


def score_job_match(job: dict, description: str, german_context: str) -> dict:
    """
    Ask Groq to score how well this job matches the candidate profile,
    and to classify the German language requirement from a focused
    excerpt (see extract_german_mentions) rather than the whole JD —
    cheaper and more reliable than asking the model to find a brief
    mention buried somewhere in a long posting.

    Returns a dict like:
        {"match_score": 8, "match_reason": "...", "german_requirement": "..."}
    On any failure, returns a safe fallback dict rather than raising —
    callers should never let a scoring failure block sending the job or
    persisting state.
    """
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_HARD_GERMAN_LEVEL_WORDS = tuple(_CONFIG["EXCLUDE_GERMAN_LEVELS"])


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


    to_send = []
    for i, job in enumerate(new_jobs):
        label = label_for_job(job)

        if i >= MAX_JOBS_SCORED_PER_RUN:

            print(f"Skipping score for '{job.get('title')}' — per-run "
                  f"scoring cap ({MAX_JOBS_SCORED_PER_RUN}) reached")
            match = {"match_score": None, "match_reason": "",
                     "german_requirement": "Unknown", "years_experience": "Not mentioned"}
            to_send.append((job, label, match))
            continue

        enriched = enrich_description(job)
        description = enriched["description"]
        german_context = enriched["german_context"]
        print(f"DEBUG scoring '{job.get('title')}' with description "
              f"length {len(description)} chars, german_context "
              f"{'found (' + str(len(german_context)) + ' chars)' if german_context else 'NOT found'}")

        try:
            match = score_job_match(job, description, german_context)
        except Exception as exc:  # noqa: BLE001
            # A scoring failure should never block the job from being sent
            print(f"WARNING scoring failed for '{job.get('title')}': {exc}",
                  file=sys.stderr)
            match = {"match_score": None, "match_reason": "",
                     "german_requirement": "Unknown", "years_experience": "Not mentioned"}

        if is_hard_german_requirement(match.get("german_requirement", "")):
            print(f"Skipped (requires {match.get('german_requirement')}): "
                  f"{job.get('title')}")
            continue

        score = match.get("match_score")
        if score is not None and score < MIN_MATCH_SCORE:
            print(f"Skipped (match score {score} < {MIN_MATCH_SCORE}): "
                  f"{job.get('title')}")
            continue

        to_send.append((job, label, match))
        time.sleep(GROQ_CALL_PACING_SECONDS)

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

            print(f"ERROR sending Telegram message for "
                  f"'{job.get('title')}': {exc}", file=sys.stderr)
        time.sleep(0.3)  # be polite to Telegram's rate limits

  
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
