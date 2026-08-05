# Data Science Job Alert Bot

Checks Adzuna every 10 minutes for new data science roles — one search
covering all of Germany, one scoped to Berlin — and sends a Telegram
message for each new listing. Runs entirely free on GitHub Actions.

## Heads up on the first run

The very first run has no history, so it will send you every currently
matching job it finds (up to 50 per search) in one burst. After that,
it only alerts on genuinely new postings. If you'd rather skip the
initial flood, edit `state/germany.json` and `state/berlin.json` before
your first run and set `last_seen_iso` to right now, e.g.
`"2026-08-04T12:00:00Z"` — that makes the first run start clean.

## 1. Get a free Adzuna API key

1. Go to https://developer.adzuna.com/ and register for a free account.
2. Once approved, your dashboard shows an `App ID` and `App Key`.
3. Keep both handy for step 3.

## 1b. Get a free Groq API key (for job match scoring)

1. Go to https://console.groq.com/ and sign up (free tier available).
2. Create an API key from the dashboard.
3. Keep it handy for step 4.

Groq scores each new job against your profile (edit `CANDIDATE_PROFILE`
in `job_alert.py` to update it) and extracts the actual German language
requirement from the job description, both shown in the Telegram message.
Uses `openai/gpt-oss-20b` — fast and cheap. If scoring fails for a given
job (rate limit, API hiccup, etc.), the job is still sent — just without
a score line — rather than being dropped or blocking the run.

## 2. Create a Telegram bot and get your chat ID

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts.
   BotFather gives you a **bot token** (looks like `123456:ABC-DEF...`).
2. Start a chat with your new bot (search its username, hit Start) and
   send it any message — this is required so it's allowed to message you.
3. Get your **chat ID**: visit this URL in a browser (replace
   `<TOKEN>` with your bot token) right after sending the message above:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Look for `"chat":{"id": ...}` in the JSON response — that number is
   your chat ID.

## 3. Push this project to GitHub

1. Create a new **private** GitHub repo (private is fine and free).
2. Push this folder's contents to it.

## 4. Add your secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these four:

| Secret name | Value |
|---|---|
| `ADZUNA_APP_ID` | from step 1 |
| `ADZUNA_APP_KEY` | from step 1 |
| `GROQ_API_KEY` | from step 1b |
| `TELEGRAM_BOT_TOKEN` | from step 2 |
| `TELEGRAM_CHAT_ID` | from step 2 |

## 5. Enable the workflow

Go to the **Actions** tab in your repo → you should see "Data Science Job
Alert" → click **Enable workflow** if prompted.

## 6. Set up the external scheduler (cron-job.org)

GitHub's own native `schedule:` cron trigger proved unreliable in testing
— it would silently stop firing for many hours at a time, especially
after any edit to the workflow file. `workflow_dispatch` (manual runs),
on the other hand, fired instantly and reliably every single time. So
instead of depending on GitHub's scheduler, we use a free external cron
service to call `workflow_dispatch` via GitHub's API every 10 minutes.

**Step A — create a GitHub Personal Access Token (PAT):**

1. Go to https://github.com/settings/personal-access-tokens/new
2. Give it a name like `job-alert-dispatch`.
3. Under "Repository access," select **Only select repositories** →
   choose `job-notify`.
4. Under "Permissions" → "Repository permissions" → set **Actions** to
   **Read and write**.
5. Generate the token and copy it somewhere safe — you won't see it
   again. Treat it like a password.

**Step B — set up cron-job.org:**

1. Go to https://cron-job.org/ and create a free account.
2. Create a new cron job with these settings:
   - **URL**:
     `https://api.github.com/repos/ranjith-p/job-notify/actions/workflows/job-alert.yml/dispatches`
   - **Request method**: POST
   - **Headers**:
     - `Authorization: Bearer <your PAT from step A>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - **Body**: `{"ref":"main"}`
   - **Schedule**: every 10 minutes, restricted to your active hours
     (cron-job.org supports timezone-aware schedules — set it to
     `Europe/Berlin`, active 9:00–23:00, so it handles daylight saving
     automatically without any manual UTC math).
3. Save and enable the job. cron-job.org shows execution history, so you
   can confirm it's actually firing and see GitHub's response.

The "Check active hours" step still exists inside the workflow itself as
a redundant safety net (in case the external schedule ever misfires
outside your intended hours), but the real scheduling now lives in
cron-job.org, not GitHub.

## Tuning it

- **Keywords**: edit the `KEYWORDS` list near the top of `job_alert.py`.
- **Frequency**: edit the `cron` line in
  `.github/workflows/job-alert.yml` (`*/10 * * * *` = every 10 min;
  GitHub's minimum supported interval is every 5 minutes, but very
  frequent schedules can be throttled/delayed by GitHub under load).
- **Locations**: add more entries to the `SEARCHES` dict in
  `job_alert.py` if you later want e.g. Munich or Hamburg too — each
  gets its own state file and its own labeled alerts automatically.
- **Adzuna free tier limits**: check current limits on your Adzuna
  dashboard — free tier is generous for this use case (a couple of
  calls every 10 minutes), but worth a glance if you add many more
  search variants.

## Why this architecture (vs. scraping LinkedIn/Indeed directly)

Adzuna is an official, documented API meant for exactly this kind of
programmatic use — no risk of account bans or broken scrapers when a
site changes its HTML. State (the "have I already alerted on this"
tracking) lives in small JSON files committed back to the repo by the
workflow itself, so nothing needs an external database.
