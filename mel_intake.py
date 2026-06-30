#!/usr/bin/env python3
"""
MEL intake: turn Slack charity-performance threads into Airtable "MEL Notes" rows.

Reads the #mel-intake channel, finds every Slack thread permalink posted there,
fetches each full thread via the Slack Web API (using a user OAuth token, so it sees
every thread the installing user can see), summarises it for MEL with Claude Sonnet 4.6,
and upserts one row per thread into the Airtable MEL Notes table (deduped on "Thread Key").

Run `python3 mel_intake.py --help` for usage. See README.md for setup.

Secrets come from the environment (e.g. GitHub Actions secrets) or, if present,
<runtime dir>/.env (default ~/.mel-intake/.env). Never hardcoded.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests  # already used by the Slack skill

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

RUNTIME_DIR = Path(os.environ.get("MEL_INTAKE_HOME", str(Path.home() / ".mel-intake")))
ENV_PATH = RUNTIME_DIR / ".env"
STATE_PATH = RUNTIME_DIR / "state.json"
LOG_PATH = RUNTIME_DIR / "mel_intake.log"

SLACK_API = "https://slack.com/api"
AIRTABLE_API = "https://api.airtable.com/v0"

# Slack errors that mean the token is invalid/insufficient -> fail loudly (DM Aidan).
SLACK_AUTH_ERRORS = {
    "invalid_auth", "not_authed", "token_revoked", "token_expired",
    "account_inactive", "no_permission", "missing_scope", "ratelimited_auth",
}
# Per-thread errors we can never recover from -> skip the link, don't block the watermark.
SLACK_SKIP_ERRORS = {
    "thread_not_found", "message_not_found", "channel_not_found", "not_in_channel",
}

log = logging.getLogger("mel_intake")


class SlackAuthError(Exception):
    """Raised when a Slack call fails because the token is invalid/insufficient."""


class SlackCallError(Exception):
    """Raised for a transient / unexpected Slack failure (retry next run)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding existing)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def setup_logging() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stderr), logging.FileHandler(LOG_PATH)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            log.warning("State file unreadable; starting fresh.")
    return {"last_processed_ts": "0", "confirmed_msg_ts": [], "self_user_id": None}


def save_state(state: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Slack Web API (direct, using the user OAuth token in SLACK_USER_TOKEN)
# ---------------------------------------------------------------------------

def _slack_token() -> str:
    tok = os.environ.get("SLACK_USER_TOKEN")
    if not tok:
        raise SlackAuthError("SLACK_USER_TOKEN is not set")
    return tok


def _slack_post(method: str, data: dict | None = None) -> dict:
    """
    One Slack Web API call with the user token.

    Raises SlackAuthError on auth failure and SlackCallError on transport failure;
    otherwise returns the parsed JSON even when ok=false (so callers can handle non-auth
    errors like channel_not_found themselves). Retries on 429.
    """
    headers = {"Authorization": f"Bearer {_slack_token()}"}
    for _ in range(6):
        try:
            r = requests.post(f"{SLACK_API}/{method}", headers=headers, data=data or {}, timeout=60)
        except Exception as e:
            raise SlackCallError(f"{method} request failed: {e}")
        if r.status_code == 429:
            wait = min(int(r.headers.get("Retry-After", "5") or "5"), 60)
            log.warning("Slack rate limited on %s; waiting %ss", method, wait)
            time.sleep(wait)
            continue
        try:
            j = r.json()
        except Exception:
            raise SlackCallError(f"{method} returned non-JSON ({r.status_code})")
        if not j.get("ok") and j.get("error") in SLACK_AUTH_ERRORS:
            raise SlackAuthError(f"Slack auth error: {j.get('error')}")
        return j
    raise SlackCallError(f"{method} still rate-limited after retries")


def _slack_paginate(method: str, data: dict, items_key: str) -> dict:
    """Follow cursor pagination, accumulating items_key. Returns {ok, <items_key>} or an error dict."""
    items, cursor = [], None
    while True:
        page = dict(data)
        if cursor:
            page["cursor"] = cursor
        j = _slack_post(method, page)
        if not j.get("ok"):
            return j
        items.extend(j.get(items_key, []) or [])
        cursor = (j.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return {"ok": True, items_key: items}


def slack(*args: str, workspace: str | None = None) -> dict:
    """
    Slack operations via the Web API. Same call shape as the old skill wrapper, so callers
    are unchanged. `workspace` is accepted for compatibility but ignored (the token is
    workspace-scoped). Raises SlackAuthError on dead/insufficient tokens.
    """
    cmd = args[0]
    if cmd == "auth":
        return _slack_post("auth.test")
    if cmd == "history":
        channel, limit = args[1], (args[2] if len(args) > 2 else "100")
        return _slack_post("conversations.history", {"channel": channel, "limit": str(limit)})
    if cmd == "replies":
        channel, ts = args[1], args[2]
        return _slack_paginate("conversations.replies", {"channel": channel, "ts": ts, "limit": "200"}, "messages")
    if cmd == "channels":
        ctype = args[1] if len(args) > 1 else "public_channel,private_channel"
        return _slack_paginate(
            "conversations.list",
            {"types": ctype, "limit": "1000", "exclude_archived": "true"}, "channels")
    if cmd == "send":
        channel, text = args[1], args[2]
        data = {"channel": channel, "text": text}
        if len(args) > 3 and args[3]:
            data["thread_ts"] = args[3]
        return _slack_post("chat.postMessage", data)
    if cmd == "user-lookup":
        j = _slack_paginate("users.list", {"limit": "200"}, "members")
        if not j.get("ok"):
            return j
        users = {}
        for u in j.get("members", []):
            uid = u.get("id")
            if not uid:
                continue
            prof = u.get("profile", {}) or {}
            users[uid] = (prof.get("display_name") or prof.get("real_name")
                          or u.get("real_name") or u.get("name") or uid)
        return {"ok": True, "users": users}
    raise SlackCallError(f"unknown slack command: {cmd}")


# ---------------------------------------------------------------------------
# Permalink parsing
# ---------------------------------------------------------------------------

PERMALINK_RE = re.compile(
    r"https?://[A-Za-z0-9.\-]+\.slack\.com/archives/(C[A-Z0-9]+)/p(\d{16,})(\?[^\s\"'<>|)]*)?"
)


def path_ts_to_dotted(digits: str) -> str:
    """p1781563508055009 -> 1781563508.055009 (insert '.' six digits from the right)."""
    return f"{digits[:-6]}.{digits[-6:]}"


def extract_threads(message: dict) -> list[dict]:
    """
    Find every Slack thread permalink the poster actually included in a message.

    Returns a list of {channel, parent_ts, url}, deduped per message. Scans the message's
    own text/attachments/blocks (catching pasted links and forwarded shares) but EXCLUDES
    `root`: a reply or thread-broadcast otherwise inherits the link from the message it is
    replying to, which double-captures the thread and mis-attributes "Captured By".
    """
    scan = {k: v for k, v in message.items() if k != "root"}
    blob = json.dumps(scan)
    seen = set()
    out = []
    for m in PERMALINK_RE.finditer(blob):
        channel, digits, query = m.group(1), m.group(2), m.group(3) or ""
        # Prefer an explicit thread_ts query param (link points at a reply).
        parent_ts = None
        if "thread_ts=" in query:
            qm = re.search(r"thread_ts=(\d+\.\d+)", query)
            if qm:
                parent_ts = qm.group(1)
        if parent_ts is None:
            parent_ts = path_ts_to_dotted(digits)
        key = f"{channel}:{parent_ts}"
        if key in seen:
            continue
        seen.add(key)
        clean_url = f"https://ceincubationprogram.slack.com/archives/{channel}/p{digits}"
        if "thread_ts=" in query:
            clean_url += f"?thread_ts={parent_ts}&cid={channel}"
        out.append({"channel": channel, "parent_ts": parent_ts, "url": clean_url})
    return out


# ---------------------------------------------------------------------------
# Name resolution / transcript building
# ---------------------------------------------------------------------------

def msg_display_name(msg: dict, users: dict) -> str:
    """Resolve a message author's display name (workspace lookup, then embedded profile)."""
    uid = msg.get("user") or msg.get("bot_id") or ""
    if uid and uid in users:
        return users[uid]
    profile = msg.get("user_profile") or {}
    name = profile.get("display_name") or profile.get("real_name") or profile.get("name")
    if name:
        return name
    if msg.get("username"):
        return msg["username"]
    return uid or "Unknown"


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
_CHANNEL_RE = re.compile(r"<#C[A-Z0-9]+\|([^>]+)>")
_LINK_RE = re.compile(r"<(https?://[^>|]+)\|([^>]+)>")
_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")
_SPECIAL_RE = re.compile(r"<!(here|channel|everyone)>")


def clean_text(text: str, users: dict) -> str:
    """Turn Slack mrkdwn into readable plain text (resolve mentions, unwrap links)."""
    if not text:
        return ""
    text = _MENTION_RE.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), text)
    text = _CHANNEL_RE.sub(lambda m: "#" + m.group(1), text)
    text = _SPECIAL_RE.sub(lambda m: "@" + m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _BARE_LINK_RE.sub(lambda m: m.group(1), text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


def build_transcript(messages: list[dict], users: dict) -> tuple[str, list[str]]:
    """Build a name-resolved transcript and the ordered list of distinct participants."""
    lines = []
    participants = []
    for msg in messages:
        if msg.get("subtype") in ("channel_join", "channel_leave"):
            continue
        name = msg_display_name(msg, users)
        body = clean_text(msg.get("text", ""), users)
        # Pull any attachment fallback text too (link previews, unfurls, app posts).
        for att in msg.get("attachments", []) or []:
            extra = att.get("text") or att.get("fallback") or ""
            extra = clean_text(extra, users)
            if extra and extra not in body:
                body = (body + "\n" + extra).strip()
        # Name any uploaded files so they aren't invisible. Contents are NOT read —
        # the model sees the filename + type and the surrounding text, not the file.
        files = msg.get("files", []) or []
        if files:
            named = []
            for f in files:
                fn = f.get("title") or f.get("name") or "file"
                ft = f.get("filetype") or f.get("mimetype") or ""
                named.append(f"{fn} ({ft})" if ft else fn)
            marker = "[attached file(s): " + "; ".join(named) + "]"
            body = (body + "\n" + marker).strip() if body else f"{marker}"
        if not body:
            continue
        if name not in participants:
            participants.append(name)
        lines.append(f"{name}: {body}")
    return "\n\n".join(lines), participants


def thread_date(parent_ts: str) -> str:
    """Date (YYYY-MM-DD, local time) of the thread's parent message."""
    try:
        return datetime.fromtimestamp(float(parent_ts)).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# PDF attachment reading (direct download with the user token)
# ---------------------------------------------------------------------------

def extract_pdf_files(messages: list[dict]) -> list[dict]:
    """Collect PDF uploads from a thread as [{name, url, size}]."""
    out = []
    for msg in messages:
        for f in msg.get("files", []) or []:
            ftype = (f.get("filetype") or "").lower()
            mime = (f.get("mimetype") or "").lower()
            if ftype == "pdf" or mime == "application/pdf":
                url = f.get("url_private_download") or f.get("url_private")
                if url:
                    out.append({
                        "name": f.get("title") or f.get("name") or "report.pdf",
                        "url": url,
                        "size": f.get("size") or 0,
                    })
    return out


def download_slack_file(url: str, max_bytes: int) -> bytes | None:
    """Download a private Slack file with the user token. Returns bytes, or None on failure."""
    headers = {"Authorization": f"Bearer {os.environ.get('SLACK_USER_TOKEN', '')}"}
    try:
        r = requests.get(url, headers=headers, timeout=90)
    except Exception as e:
        log.warning("PDF download error for %s: %s", url[:80], e)
        return None
    if not r.ok:
        log.warning("PDF download HTTP %s for %s", r.status_code, url[:80])
        return None
    if "html" in r.headers.get("Content-Type", "").lower():
        log.warning("PDF download returned an HTML page (auth failed?) for %s", url[:80])
        return None
    data = r.content
    if len(data) > max_bytes:
        log.warning("PDF exceeds %d-byte cap (%d); skipping %s", max_bytes, len(data), url[:80])
        return None
    return data


def pdf_to_text(raw: bytes, max_chars: int) -> str:
    """Extract text from a PDF, truncated to max_chars.

    Resilient to per-page errors: some PDFs raise (e.g. KeyError('bbox')) on certain
    pages, so we extract page by page and skip only the bad ones. Returns '' only if the
    file can't be opened or no page yields text.
    """
    try:
        import io
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
    except Exception as e:
        log.warning("PDF open failed (%s).", e)
        return ""
    parts, total, bad = [], 0, 0
    for page in reader.pages:
        try:
            chunk = (page.extract_text() or "").strip()
        except Exception:
            bad += 1
            continue
        if not chunk:
            continue
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    if bad:
        log.warning("PDF: skipped %d page(s) that failed extraction.", bad)
    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[...report text truncated...]"
    return text


# ---------------------------------------------------------------------------
# Summarisation (Claude Sonnet 4.6)
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = (
    "You are a monitoring, evaluation & learning (MEL) analyst for Ambitious Impact "
    "(AIM), a charity incubator. You are given the full text of a Slack thread in "
    "which a charity (or AIM staff) reports on the charity's performance. Summarise "
    "it tightly for a MEL tracker.\n\n"
    "Capture, when present: what the charity is reporting; progress since the last "
    "update; any metrics or numbers (keep them exact); blockers and risks; and any "
    "asks or decisions needed. Omit pleasantries, reactions, and congratulations. Be "
    "concise (a short paragraph or a few bullet-style lines). Do not invent anything "
    "not in the thread. If report text extracted from an attached PDF is included, use "
    "its contents (results, numbers, tables) as well as the thread text.\n\n"
    "Also identify which charity the thread is about, plus a very short focus line. "
    "Respond with ONLY a JSON object, no prose, no code fences, with exactly these keys:\n"
    '  "summary": string  (the MEL summary, plain text)\n'
    '  "focus": string  (UNDER 10 words naming the thread\'s focus areas, semicolon-separated, '
    'e.g. "Pilot results; driving teacher uptake")\n'
    '  "charity_name": string or null  (the charity\'s full name if identifiable, else null)\n'
    '  "charity_acronym": string or null  (its short name/acronym if used, else null)'
)


def summarise(transcript: str, channel_name: str, api_key: str, model: str,
              pdf_docs: list | None = None) -> dict:
    """Call Claude to produce {summary, focus, charity_name, charity_acronym}.

    pdf_docs is an optional list of (filename, extracted_text) for attached reports.
    Extracted text is sent (not the raw PDF) to stay within tight per-minute token limits.
    """
    import anthropic  # imported lazily so --dry-run without the SDK still parses

    # max_retries lets the SDK wait out 429s (low-tier orgs have a tight tokens/min limit;
    # it backs off on retry-after and retries).
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)

    # Cap the transcript so transcript + report text stay under the per-minute token limit.
    t = transcript if len(transcript) <= 16000 else transcript[:16000] + "\n[...transcript truncated...]"
    reports = ""
    for name, txt in (pdf_docs or []):
        reports += f"\n\n--- Attached report text ({name}) ---\n{txt}"
    user_msg = (
        f"Slack channel: {channel_name}\n\n"
        f"Thread transcript:\n\n{t}"
        + (reports + "\n\n" if reports else "\n\n")
        + "Return the JSON object now."
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    return parse_summary_json(text)


def parse_summary_json(text: str) -> dict:
    """Parse the model's JSON output leniently (tolerate code fences / stray text)."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not m:
            return {"summary": text.strip(), "focus": "", "charity_name": None, "charity_acronym": None}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"summary": text.strip(), "focus": "", "charity_name": None, "charity_acronym": None}
    return {
        "summary": (data.get("summary") or "").strip(),
        "focus": (data.get("focus") or "").strip(),
        "charity_name": (data.get("charity_name") or None),
        "charity_acronym": (data.get("charity_acronym") or None),
    }


# ---------------------------------------------------------------------------
# Airtable
# ---------------------------------------------------------------------------

def airtable_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_charities(token: str, base_id: str, charities_table: str) -> list[dict]:
    """Return [{id, name}] for every Charities record (for confident auto-linking)."""
    records = []
    offset = None
    url = f"{AIRTABLE_API}/{base_id}/{charities_table}"
    while True:
        params = {"fields[]": "Name", "pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(url, headers=airtable_headers(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for rec in body.get("records", []):
            name = (rec.get("fields", {}) or {}).get("Name")
            if name:
                records.append({"id": rec["id"], "name": name})
        offset = body.get("offset")
        if not offset:
            break
        time.sleep(0.2)
    return records


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def match_charity(name: str | None, acronym: str | None, charities: list[dict]) -> str | None:
    """
    Confidently match a charity name/acronym to a Charities record id.

    Charities are named "Full name (Acronym)". We match on exact acronym, then exact
    full-name. If zero or multiple records match, we link nothing (a wrong link is
    worse than no link).
    """
    if not charities:
        return None
    parsed = []
    for c in charities:
        full = c["name"]
        acr = None
        pm = re.search(r"\(([^)]+)\)\s*$", full)
        if pm:
            acr = pm.group(1)
            full = full[: pm.start()].strip()
        parsed.append({"id": c["id"], "full_norm": _norm(full), "acr_norm": _norm(acr) if acr else None})

    if acronym:
        a = _norm(acronym)
        hits = [p["id"] for p in parsed if p["acr_norm"] and p["acr_norm"] == a]
        if len(hits) == 1:
            return hits[0]
    if name:
        n = _norm(name)
        hits = [p["id"] for p in parsed if p["full_norm"] == n]
        if len(hits) == 1:
            return hits[0]
        # Also allow the model's name to match a record's acronym exactly.
        hits = [p["id"] for p in parsed if p["acr_norm"] and p["acr_norm"] == n]
        if len(hits) == 1:
            return hits[0]
    return None


def upsert_records(token: str, base_id: str, table: str, records: list[dict]) -> None:
    """Upsert rows, deduping on 'Thread Key'. Chunks of 10, throttled to <5 req/s/base."""
    url = f"{AIRTABLE_API}/{base_id}/{table}"
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        payload = {
            "performUpsert": {"fieldsToMergeOn": ["Thread Key"]},
            "records": [{"fields": f} for f in chunk],
        }
        for attempt in range(4):
            r = requests.patch(url, headers=airtable_headers(token), json=payload, timeout=60)
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("Airtable rate limited; retrying in %ss", wait)
                time.sleep(wait)
                continue
            if not r.ok:
                raise RuntimeError(f"Airtable upsert failed ({r.status_code}): {r.text[:500]}")
            break
        else:
            raise RuntimeError("Airtable upsert failed after retries (429)")
        time.sleep(0.25)  # stay under 5 requests/sec/base


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def macos_notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def alert(message: str, workspace: str, state: dict) -> None:
    """Fail loudly: log, macOS notification, and best-effort Slack DM to Aidan."""
    log.error("ALERT: %s", message)
    macos_notify("MEL intake failed", message)
    target = state.get("self_user_id") or os.environ.get("SLACK_ALERT_USER_ID")
    if not target:
        log.error("No Slack DM target known; cannot DM. Set SLACK_ALERT_USER_ID in .env.")
        return
    try:
        slack("send", target, f":warning: MEL intake job problem: {message}", workspace=workspace)
        log.info("Sent failure DM to Slack user %s", target)
    except Exception as e:  # if tokens are dead, the DM dies too — that's why we also notify/log
        log.error("Could not send failure DM (%s). The log + macOS notification are the fallback.", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"Missing required env var {name} (set it in {ENV_PATH}).")
    return val


def main() -> int:
    ap = argparse.ArgumentParser(description="Slack #mel-intake -> Airtable MEL Notes.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Process and summarise, but do not write to Airtable or post Slack confirmations.")
    ap.add_argument("--limit", type=int, default=None, help="Override how many intake messages to read.")
    ap.add_argument("--since-days", type=int, default=None, help="Force a lookback window (days) instead of the state file.")
    ap.add_argument("--force", action="store_true", help="Run even if a successful run already happened today.")
    args = ap.parse_args()

    setup_logging()
    load_env_file(ENV_PATH)

    workspace = os.environ.get("SLACK_WORKSPACE", "ceincubationprogram")
    intake_channel = os.environ.get("MEL_INTAKE_CHANNEL", "C0B9WG6UY4W")
    base_id = os.environ.get("AIRTABLE_BASE_ID", "app6tmBJhcfCS7FLs")
    table_id = os.environ.get("AIRTABLE_TABLE_ID", "tblbtQqlp4z9uky47")
    charities_table = os.environ.get("AIRTABLE_CHARITIES_TABLE_ID", "tblSsWP0lp1fH9kk6")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    captured_by = os.environ.get("MEL_CAPTURED_BY", "MEL intake script")
    history_limit = args.limit or int(os.environ.get("SLACK_HISTORY_LIMIT", "200"))
    lookback_days = args.since_days if args.since_days is not None else int(os.environ.get("MEL_FIRST_RUN_LOOKBACK_DAYS", "30"))
    link_charities = os.environ.get("MEL_LINK_CHARITIES", "true").lower() not in ("0", "false", "no")
    read_pdfs = os.environ.get("MEL_READ_PDFS", "true").lower() not in ("0", "false", "no")
    pdf_max_bytes = int(os.environ.get("MEL_PDF_MAX_MB", "25")) * 1_000_000
    pdf_text_maxchars = int(os.environ.get("MEL_PDF_TEXT_MAXCHARS", "12000"))  # ~3k tokens
    max_pdfs = int(os.environ.get("MEL_MAX_PDFS", "5"))

    require_env("SLACK_USER_TOKEN")
    anthropic_key = require_env("ANTHROPIC_API_KEY")
    airtable_token = os.environ.get("AIRTABLE_API_KEY")
    if not airtable_token and not args.dry_run:
        raise SystemExit(f"Missing required env var AIRTABLE_API_KEY (set it in {ENV_PATH}).")

    state = load_state()

    # No runs on Saturdays. The launchd job still fires; the script just no-ops.
    # Anything pasted on Saturday is captured on the next run (Sunday). Monday=0..Sun=6.
    if not args.force and not args.dry_run and datetime.now().weekday() == 5:
        log.info("Saturday — not running today (links will be picked up on the next run). "
                 "Use --force to override.")
        return 0

    # Run at most once per local day: the launchd job fires several times (first at
    # 10:00, then retries through the afternoon) so a sleeping/closed Mac still gets a
    # run, but we skip once a run has already succeeded today.
    today = datetime.now().strftime("%Y-%m-%d")
    if not args.force and not args.dry_run and state.get("last_run_date") == today:
        log.info("Already ran successfully today (%s); skipping. Use --force to run anyway.", today)
        return 0

    # --- 1. Auth check (the loud-failure tripwire for dead browser tokens) ---
    try:
        auth = slack("auth", workspace=workspace)
        state["self_user_id"] = auth.get("user_id") or state.get("self_user_id")
        log.info("Slack auth OK as %s on %s.", auth.get("user"), auth.get("url"))
    except SlackAuthError as e:
        alert(f"Slack auth failed ({e}). Check/refresh SLACK_USER_TOKEN for the 'Mel intake' "
              f"Slack app (api.slack.com/apps -> OAuth & Permissions -> reinstall).", workspace, state)
        save_state(state)
        return 1
    except SlackCallError as e:
        alert(f"Slack auth check failed: {e}", workspace, state)
        save_state(state)
        return 1

    # --- 2. Name + channel lookups ---
    try:
        users = slack("user-lookup", workspace=workspace).get("users", {})
    except (SlackAuthError, SlackCallError) as e:
        alert(f"Could not load Slack user directory: {e}", workspace, state)
        save_state(state)
        return 1
    log.info("Resolved %d Slack users.", len(users))

    # Query each type separately and merge (covers both public and private channels).
    channel_names = {}
    for ctype in ("public_channel", "private_channel"):
        try:
            ch = slack("channels", ctype, workspace=workspace)
            for c in ch.get("channels", []):
                channel_names[c["id"]] = "#" + c.get("name", c["id"])
        except (SlackAuthError, SlackCallError) as e:
            log.warning("Channel name lookup failed for %s (%s); falling back to IDs.", ctype, e)
    log.info("Resolved %d channel names.", len(channel_names))

    # --- 3. Read intake channel since the watermark ---
    last_ts = float(state.get("last_processed_ts") or 0)
    since_ts = last_ts if last_ts > 0 else (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    log.info("Reading #mel-intake (%s) since ts=%.6f (limit %d).", intake_channel, since_ts, history_limit)

    try:
        hist = slack("history", intake_channel, str(history_limit), workspace=workspace)
    except (SlackAuthError, SlackCallError) as e:
        alert(f"Could not read #mel-intake history: {e}", workspace, state)
        save_state(state)
        return 1

    messages = [m for m in hist.get("messages", []) if float(m.get("ts", 0)) > since_ts]
    messages.sort(key=lambda m: float(m.get("ts", 0)))
    log.info("%d new intake message(s) to scan.", len(messages))

    charities = []
    if link_charities and airtable_token:
        try:
            charities = fetch_charities(airtable_token, base_id, charities_table)
            log.info("Loaded %d charities for auto-linking.", len(charities))
        except Exception as e:
            log.warning("Could not load Charities table for auto-linking (%s); skipping links.", e)

    confirmed = set(state.get("confirmed_msg_ts", []))
    watermark = state.get("last_processed_ts") or "0"
    contiguous_ok = True
    stats = {"threads": 0, "rows": 0, "skipped": 0, "errors": 0}

    for msg in messages:
        msg_ts = msg.get("ts")
        poster = msg_display_name(msg, users)  # who pasted/forwarded the link into #mel-intake
        threads = extract_threads(msg)
        if not threads:
            if contiguous_ok:
                watermark = msg_ts
            continue

        log.info("Intake message %s -> %d thread link(s).", msg_ts, len(threads))
        records = []
        message_had_failure = False  # transient failure -> block watermark, retry next run

        for t in threads:
            channel, parent_ts, url = t["channel"], t["parent_ts"], t["url"]
            thread_key = f"{channel}:{parent_ts}"
            try:
                rep = slack("replies", channel, parent_ts, workspace=workspace)
            except SlackAuthError:
                raise  # bubble to outer handler -> alert + exit
            except SlackCallError as e:
                log.error("replies failed for %s (%s); will retry next run.", thread_key, e)
                message_had_failure = True
                stats["errors"] += 1
                continue

            if rep.get("ok") is False:
                err = rep.get("error", "unknown")
                if err in SLACK_AUTH_ERRORS:
                    raise SlackAuthError(f"Slack auth error on replies: {err}")
                if err in SLACK_SKIP_ERRORS:
                    log.warning("Thread %s inaccessible (%s); skipping (won't retry).", thread_key, err)
                    stats["skipped"] += 1
                    continue
                log.error("replies error for %s (%s); will retry next run.", thread_key, err)
                message_had_failure = True
                stats["errors"] += 1
                continue

            thread_msgs = rep.get("messages", []) or []
            if not thread_msgs:
                log.warning("Thread %s returned no messages; skipping.", thread_key)
                stats["skipped"] += 1
                continue

            transcript, participants = build_transcript(thread_msgs, users)
            if not transcript.strip():
                log.warning("Thread %s had no usable text; skipping.", thread_key)
                stats["skipped"] += 1
                continue

            channel_name = channel_names.get(channel, channel)

            # Download any PDF attachments, extract their text (capped), feed into the summary.
            pdf_docs = []
            if read_pdfs:
                budget = pdf_text_maxchars
                for pf in extract_pdf_files(thread_msgs)[:max_pdfs]:
                    if budget <= 0:
                        break
                    raw = download_slack_file(pf["url"], pdf_max_bytes)
                    if not raw:
                        continue
                    txt = pdf_to_text(raw, budget)
                    if txt:
                        pdf_docs.append((pf["name"], txt))
                        budget -= len(txt)

            try:
                summary = summarise(transcript, channel_name, anthropic_key, model, pdf_docs)
                if pdf_docs:
                    log.info("Read %d PDF(s) into the summary for %s.", len(pdf_docs), thread_key)
            except Exception as e:
                if pdf_docs:
                    log.warning("Summary with %d PDF(s) failed for %s (%s); retrying text-only.",
                                len(pdf_docs), thread_key, e)
                    try:
                        summary = summarise(transcript, channel_name, anthropic_key, model, [])
                    except Exception as e2:
                        log.error("Summarisation failed for %s (%s); will retry next run.", thread_key, e2)
                        message_had_failure = True
                        stats["errors"] += 1
                        continue
                else:
                    log.error("Summarisation failed for %s (%s); will retry next run.", thread_key, e)
                    message_had_failure = True
                    stats["errors"] += 1
                    continue

            fields = {
                "Thread Key": thread_key,
                "Thread URL": url,
                "Channel": channel_name,
                "Summary": summary["summary"],
                "Focus": " ".join((summary.get("focus") or "").split()[:12]),
                "Participants": ", ".join(participants),
                "Date": thread_date(parent_ts),
                "Raw Messages": transcript[:99000],  # Airtable long-text safety margin
                "Captured By": poster if poster and poster != "Unknown" else captured_by,
            }
            if link_charities:
                rec_id = match_charity(summary.get("charity_name"), summary.get("charity_acronym"), charities)
                if rec_id:
                    fields["Charity"] = [rec_id]
                else:
                    log.info("No confident charity match for %s (name=%r acr=%r); leaving blank.",
                             thread_key, summary.get("charity_name"), summary.get("charity_acronym"))

            records.append(fields)
            stats["threads"] += 1
            if args.dry_run:
                log.info("[dry-run] would upsert %s:\n%s", thread_key, json.dumps(fields, indent=2, ensure_ascii=False))

        # Write this message's rows.
        wrote_ok = False
        if records and not args.dry_run:
            try:
                upsert_records(airtable_token, base_id, table_id, records)
                stats["rows"] += len(records)
                wrote_ok = True
                log.info("Upserted %d row(s) for intake message %s.", len(records), msg_ts)
            except Exception as e:
                log.error("Airtable upsert failed for intake message %s (%s); will retry next run.", msg_ts, e)
                message_had_failure = True
                stats["errors"] += 1
        elif records and args.dry_run:
            wrote_ok = True  # pretend, for confirmation/watermark logic in dry-run logs

        # Threaded confirmation reply (default API-summary path only; never in dry-run).
        if wrote_ok and records and not args.dry_run and msg_ts not in confirmed:
            try:
                slack("send", intake_channel, "MEL bot says: Captured to the Airtable", msg_ts, workspace=workspace)
                confirmed.add(msg_ts)
                log.info("Posted confirmation reply to intake message %s.", msg_ts)
            except SlackAuthError:
                raise
            except Exception as e:
                log.warning("Confirmation reply failed for %s (%s); row is saved, not a job failure.", msg_ts, e)
                confirmed.add(msg_ts)  # row is saved; don't risk duplicate replies on retry

        # Advance the contiguous watermark only while nothing has failed.
        if message_had_failure:
            contiguous_ok = False
        elif contiguous_ok:
            watermark = msg_ts

    state["confirmed_msg_ts"] = sorted(confirmed)[-500:]  # keep the list bounded
    if not args.dry_run:
        state["last_processed_ts"] = watermark
        state["last_run_date"] = today  # mark today done so later scheduled attempts skip
    save_state(state)

    log.info("Done. threads=%d rows=%d skipped=%d errors=%d watermark=%s%s",
             stats["threads"], stats["rows"], stats["skipped"], stats["errors"],
             watermark, " (dry-run: watermark not advanced)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SlackAuthError as e:
        # Last-resort: a thread-level auth error bubbled all the way up.
        st = load_state()
        ws = os.environ.get("SLACK_WORKSPACE", "ceincubationprogram")
        setup_logging()
        alert(f"Slack auth failed mid-run ({e}). Check/refresh SLACK_USER_TOKEN.", ws, st)
        save_state(st)
        sys.exit(1)
