#!/usr/bin/env python3
"""Daily fun facts Telegram bot.

Picks a random topic from topics.txt, asks Google Gemini for one short,
genuinely NEW interesting fun fact about it (de-duplicated against history),
and sends it to a Telegram chat in a compact, plain-text format.

Designed to run once per invocation (e.g. from a GitHub Actions cron) — there
is no long-running server. Fact history is persisted in-repo
(data/history.jsonl) and committed back by the workflow after a successful
send. Exits 0 on success and non-zero only on a real model/delivery failure.
"""

import datetime
import hashlib
import html
import json
import logging
import os
import random
import re
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
HISTORY_PATH = os.path.join(SCRIPT_DIR, "data", "history.jsonl")

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

REQUEST_TIMEOUT = 60

# How many prior same-topic facts to show the model / cap the avoid-list at.
AVOID_LIST_SIZE = 30
# How many retries when a generated fact duplicates something already sent.
MAX_DEDUP_RETRIES = 4

PROMPT_TEMPLATE = (
    "Give me ONE genuinely interesting, surprising fun fact about the topic: "
    "\"{topic}\".\n\n"
    "Return ONLY a compact JSON object, nothing else, in exactly this shape:\n"
    "{{\"title\": \"...\", \"fact\": \"...\"}}\n\n"
    "Rules:\n"
    "- title: a short 3-6 word headline / hook (e.g. \"The Great Banyan "
    "Tree\").\n"
    "- fact: 2-3 sentences, CRISP, HARD MAX ~60 words.\n"
    "- Plain text only. NO Markdown, NO asterisks, NO HTML tags, no bullet "
    "points, no preamble.\n"
    "- For any Bengali or Hindi word/name/term, write it inline as: "
    "NATIVE_SCRIPT (transliteration, \"meaning\") in plain text, e.g. "
    "\u09a8\u09a6\u09c0 (nodi, \"river\").\n"
    "- Prefer surprising specifics over common, well-known trivia."
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


def normalize_fact(fact):
    """Normalize a fact for hashing: lowercase, strip punctuation, collapse ws."""
    text = fact.lower().strip()
    # Drop anything that is not a word char or whitespace (Unicode-aware).
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fact_hash(fact):
    return hashlib.sha256(normalize_fact(fact).encode("utf-8")).hexdigest()


def load_history(path=HISTORY_PATH):
    """Return list of history entry dicts. Missing/blank file -> []."""
    entries = []
    if not os.path.isfile(path):
        return entries
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    log.warning("Skipping malformed history line")
    except OSError as exc:
        log.warning("Could not read history %s: %s", path, exc)
    return entries


def append_history(entry, path=HISTORY_PATH):
    """Append one JSON entry as a line, creating the data/ dir if needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def build_avoid_list(history, topic, limit=AVOID_LIST_SIZE):
    """Most-recent `limit` prior entries for `topic` as short 'avoid' strings."""
    same_topic = [e for e in history if e.get("topic") == topic]
    recent = same_topic[-limit:]
    avoid = []
    for e in recent:
        title = (e.get("title") or "").strip()
        first_words = " ".join((e.get("fact") or "").split()[:12])
        avoid.append("{} — {}".format(title, first_words).strip(" —"))
    return avoid


def build_prompt(topic, avoid_list):
    prompt = PROMPT_TEMPLATE.format(topic=topic)
    if avoid_list:
        bullets = "\n".join("- " + a for a in avoid_list if a)
        prompt += (
            "\n\nAlready shared for this topic — do NOT repeat or closely "
            "paraphrase any of these. Produce a genuinely NEW fact:\n" + bullets
        )
    return prompt


def _extract_json_object(text):
    """Best-effort extraction of the first {...} JSON object from model text."""
    stripped = text.strip()
    # Remove ```json ... ``` or ``` ... ``` fences.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped,
                     flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    # Grab the first balanced-looking {...} block (greedy to last brace).
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = stripped[start:end + 1]
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    return None


def parse_model_text(text, topic):
    """Return (title, fact) from model output, robust to fences/prose.

    Falls back to title=topic and the raw stripped text (asterisks removed)
    when no valid JSON object is found.
    """
    obj = _extract_json_object(text)
    if isinstance(obj, dict) and obj.get("fact"):
        title = str(obj.get("title") or topic).strip()
        fact = str(obj.get("fact")).strip()
    else:
        title = topic
        fact = text.strip()
    # Belt-and-braces: strip stray Markdown asterisks the model may emit.
    title = title.replace("*", "").strip()
    fact = fact.replace("*", "").strip()
    return title, fact


def fetch_fact_raw(prompt, api_key, model):
    """Call Gemini once and return raw text, or exit(1) on hard failure."""
    url = GEMINI_URL.format(model=model, key=api_key)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        log.error("Gemini request failed: %s", exc)
        sys.exit(1)

    if resp.status_code != 200:
        log.error("Gemini returned HTTP %s: %s", resp.status_code, resp.text)
        sys.exit(1)

    try:
        data = resp.json()
    except ValueError:
        log.error("Gemini response was not valid JSON: %s", resp.text)
        sys.exit(1)

    candidates = data.get("candidates") or []
    if not candidates:
        log.error("Gemini returned no candidates: %s", resp.text)
        sys.exit(1)

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()

    if not text:
        log.error(
            "Gemini returned empty output (finishReason=%s): %s",
            finish_reason, resp.text,
        )
        sys.exit(1)

    return text


def generate_unique_fact(topic, api_key, model, history):
    """Generate a (title, fact, hash) that is not already in history.

    Retries up to MAX_DEDUP_RETRIES, growing the avoid-list with each
    just-produced duplicate. After exhausting retries, returns the last fact
    anyway (a rare repeat beats sending nothing).
    """
    seen = {e.get("hash") for e in history if e.get("hash")}
    avoid = build_avoid_list(history, topic)

    title = fact = fhash = None
    for attempt in range(1, MAX_DEDUP_RETRIES + 1):
        prompt = build_prompt(topic, avoid)
        raw = fetch_fact_raw(prompt, api_key, model)
        title, fact = parse_model_text(raw, topic)
        fhash = fact_hash(fact)
        if fhash not in seen:
            log.info("Got a new fact on attempt %d", attempt)
            return title, fact, fhash
        log.warning("Attempt %d produced a duplicate fact; retrying", attempt)
        # Feed the duplicate back so the model avoids it next time.
        avoid.append("{} — {}".format(
            title, " ".join(fact.split()[:12])).strip(" —"))

    log.warning(
        "Still duplicate after %d attempts; sending it anyway",
        MAX_DEDUP_RETRIES,
    )
    return title, fact, fhash


def compose_message(topic, title, fact):
    """Compact 3-line HTML message; every field html.escape'd (plain text)."""
    return "\U0001f4a1 <b>{title}</b>\n<i>{topic}</i>\n\n{fact}".format(
        title=html.escape(title),
        topic=html.escape(topic),
        fact=html.escape(fact),
    )


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
        log.error("Telegram returned HTTP %s: %s", resp.status_code, resp.text)
        sys.exit(1)

    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram reported an error: %s", resp.text)
        sys.exit(1)

    return data.get("result", {}).get("message_id")


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

    history = load_history()
    topic = random.choice(topics)
    log.info("Chosen topic: %s", topic)
    log.info("Using Gemini model: %s", model)
    log.info("History entries loaded: %d", len(history))

    title, fact, fhash = generate_unique_fact(topic, api_key, model, history)
    log.info("Fact title: %s", title)
    log.info("Fact (%d chars, ~%d words)", len(fact), len(fact.split()))

    message = compose_message(topic, title, fact)
    message_id = send_telegram(token, chat_id, message)
    log.info("Telegram message sent (message_id=%s)", message_id)

    # Only record history after a confirmed successful send.
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "topic": topic,
        "title": title,
        "fact": fact,
        "hash": fhash,
    }
    append_history(entry)
    log.info("Appended entry to %s", HISTORY_PATH)

    return 0


if __name__ == "__main__":
    sys.exit(main())
