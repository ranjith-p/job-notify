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
| `TELEGRAM_BOT_TOKEN` | from step 2 |
| `TELEGRAM_CHAT_ID` | from step 2 |

## 5. Enable the workflow

Go to the **Actions** tab in your repo → you should see "Data Science Job
Alert" → click **Enable workflow** if prompted. It will now run every
10 minutes automatically. You can also trigger a manual run any time via
**Actions → Data Science Job Alert → Run workflow**.

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
