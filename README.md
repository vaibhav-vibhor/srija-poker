# Daily Fun Facts Telegram Bot

A tiny, dependency-light bot that sends a short, interesting fun fact to a
Telegram chat several times a day. Each run picks a random topic from
[`topics.txt`](./topics.txt), asks **Google Gemini** (free tier) for one
concise fun fact about it, and delivers it via the Telegram Bot API.

There is **no server** — it runs entirely on GitHub Actions cron.

## Schedule

The workflow ([`.github/workflows/facts.yml`](./.github/workflows/facts.yml))
runs six times a day:

| IST (local) | UTC (cron)      |
| ----------- | --------------- |
| 11:00       | `30 5 * * *`    |
| 14:30       | `0 9 * * *`     |
| 16:30       | `0 11 * * *`    |
| 19:30       | `0 14 * * *`    |
| 21:30       | `0 16 * * *`    |
| 23:30       | `0 18 * * *`    |

> **Note:** GitHub Actions cron is best-effort. Runs may start a few minutes
> late, and under heavy load a scheduled slot can occasionally be skipped.
> That's perfectly fine for this use case.

You can also trigger a run manually — see [Manual runs](#manual-runs).

## Editing topics

Edit [`topics.txt`](./topics.txt), one topic per line. Blank lines and lines
starting with `#` are ignored, and surrounding whitespace is stripped, so you
can freely comment and reformat. Add as many topics as you like.

## Required GitHub Actions secrets

Set these under **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret               | How to get it |
| -------------------- | ------------- |
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot with `/newbot`, and copy the token. |
| `CHAT_ID`            | Send any message to your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`. |
| `GEMINI_API_KEY`     | Create a free API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey). |

Optional: set a `GEMINI_MODEL` repository **variable** (or env var locally) to
override the model. The default is `gemini-2.0-flash`.

## Running locally

1. Create a `.env` from the template:
   ```bash
   cp .env.example .env
   ```
2. Fill in `TELEGRAM_BOT_TOKEN`, `CHAT_ID`, and `GEMINI_API_KEY`.
3. Install dependencies and run:
   ```bash
   pip install -r requirements.txt
   python bot.py
   ```

`bot.py` loads `.env` automatically if present (a small built-in parser — no
extra dependency). A missing `.env` is fine; it just falls back to real
environment variables.

## Manual runs

Go to the repository's **Actions** tab, select the **Daily fun facts**
workflow, and click **Run workflow** (this uses the `workflow_dispatch`
trigger). This is the easiest way to test end-to-end once the three secrets
are set.

## How it works

1. Read and parse `topics.txt` (path relative to the script, so CWD doesn't
   matter).
2. Pick one topic at random.
3. Ask Gemini via REST (`generateContent`) for a single fun fact — Bengali/Hindi
   terms are returned in native script with a transliteration and gloss.
4. Compose an HTML message: a bold topic header plus the (HTML-escaped) fact.
5. Send it with Telegram `sendMessage` (`parse_mode=HTML`).

The script exits `0` on success and non-zero only on a real model or delivery
failure, so failed runs show up clearly in the Actions log.
