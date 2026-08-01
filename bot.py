#!/usr/bin/env python3
"""Daily fun facts Telegram bot.

Picks a random topic from topics.txt, asks Google Gemini for one short
interesting fun fact about it, and sends that fact to a Telegram chat.

Designed to run once per invocation (e.g. from a GitHub Actions cron) —
there is no long-running server. Exits 0 on success and non-zero only on a
real model or delivery failure.
"""

import html
import logging
import os
import random
import sys

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("funfacts")

# Directory of this script, so file/CWD independent lookups work everywhere.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOPICS_PATH = os.path.join(SCRIPT_DIR, "topics.txt")

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

REQUEST_TIMEOUT = 60

PROMPT_TEMPLATE = (
    "Give me ONE concise, genuinely interesting fun fact about the topic: "
    "\"{topic}\".\n\n"
    "Requirements:\n"
    "- About 2-4 sentences, under ~120 words.\n"
    "- Clear English for a curious general reader.\n"
    "- Prefer surprising specifics over common, well-known trivia.\n"
    "- If the fact involves a Bengali or Hindi word/name/term, include it in "
    "its native script followed by a Latin transliteration and a short gloss "
    "in parentheses, e.g. \"\u09a8\u09a6\u09c0 (nodi, 'river')\".\n"
    "- Return ONLY the fact text. No preamble such as \"Sure! Here's a "
    "fact\", no headings, no bullet points."
)


def load_env_file():
    """Load KEY=VALUE pairs from a local .env if present.

    Tiny hand-rolled parser so there is no hard dependency on python-dotenv.
    Existing environment variables are never overwritten. A missing .env is
    perfectly fine (the normal case in CI).
    """
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        log.info("Loaded local .env file")
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("Could not read .env file: %s", exc)


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def load_topics(path=TOPICS_PATH):
    """Return topics from the file, ignoring blank and '#' comment lines."""
    topics = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                topics.append(line)
    except OSError as exc:
        log.error("Could not read topics file %s: %s", path, exc)
        sys.exit(1)
    return topics


def fetch_fact(topic, api_key, model):
    """Call Gemini and return the generated fact text, or exit(1) on failure."""
    url = GEMINI_URL.format(model=model, key=api_key)
    prompt = PROMPT_TEMPLATE.format(topic=topic)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        log.error("Gemini request failed: %s", exc)
        sys.exit(1)

    if resp.status_code != 200:
        log.error(
            "Gemini returned HTTP %s: %s", resp.status_code, resp.text
        )
        sys.exit(1)

    try:
        data = resp.json()
    except ValueError:
        log.error("Gemini response was not valid JSON: %s", resp.text)
        sys.exit(1)

    candidates = data.get("candidates") or []
    if not candidates:
        # e.g. prompt blocked by safety filters
        log.error("Gemini returned no candidates: %s", resp.text)
        sys.exit(1)

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()

    if not text:
        log.error(
            "Gemini returned an empty fact (finishReason=%s): %s",
            finish_reason,
            resp.text,
        )
        sys.exit(1)

    return text


def send_telegram(token, chat_id, message):
    """Send an HTML message via Telegram, returning message_id or exit(1)."""
    url = TELEGRAM_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)
        sys.exit(1)

    if resp.status_code != 200:
        log.error(
            "Telegram returned HTTP %s: %s", resp.status_code, resp.text
        )
        sys.exit(1)

    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram reported an error: %s", resp.text)
        sys.exit(1)

    return data.get("result", {}).get("message_id")


def compose_message(topic, fact):
    """Bold topic header + HTML-escaped fact body."""
    return "<b>\U0001f4a1 {topic}</b>\n\n{fact}".format(
        topic=html.escape(topic),
        fact=html.escape(fact),
    )


def main():
    load_env_file()

    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("CHAT_ID")
    api_key = require_env("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL

    topics = load_topics()
    if not topics:
        log.error("No topics found in %s", TOPICS_PATH)
        sys.exit(1)

    topic = random.choice(topics)
    log.info("Chosen topic: %s", topic)
    log.info("Using Gemini model: %s", model)

    fact = fetch_fact(topic, api_key, model)
    log.info("Fetched fact (%d chars)", len(fact))

    message = compose_message(topic, fact)
    message_id = send_telegram(token, chat_id, message)
    log.info("Telegram message sent (message_id=%s)", message_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
