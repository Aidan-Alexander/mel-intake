#!/usr/bin/env python3
"""
Newsletter intake: charity newsletters forwarded into #mel-intake -> Airtable "Newsletters".

Morgan forwards newsletter subscriptions (from research@charityentrepreneurship.com) into
#mel-intake via Slack's email-to-channel integration. Those land as Slackbot messages whose
`files[]` contains a `filetype:"email"` object carrying `subject`, `from`, and the full
`plain_text` body inline. This job reads them, skips Google forwarding-confirmation emails,
summarises each newsletter with Claude Sonnet 4.6, and upserts one row per newsletter into
the Airtable Newsletters table — deduped on "Source Msg TS" (the Slack message ts).

Reuses the Slack Web API layer, Airtable helpers, and charity matching from mel_intake.py.
No digest (that's a separate, later job). Run `python3 newsletter_intake.py --help`.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import mel_intake as mel  # shared: slack(), fetch_charities(), match_charity(), upsert_records(), etc.

log = logging.getLogger("newsletter_intake")

# Email senders / subjects that are plumbing, not newsletters -> skip.
SKIP_SENDERS = {"forwarding-noreply@google.com"}
SKIP_SUBJECT_RE = re.compile(r"forwarding confirmation", re.IGNORECASE)

_BROWSER_LINK_RE = re.compile(
    r"view\s+(?:this\s+|the\s+)?(?:e-?mail\s+|newsletter\s+)?"
    r"(?:in\s+(?:your\s+)?browser|online"       # ...in your browser / online
    r"|(?:by\s+)?click(?:ing)?\s+here"          # ...(by) clicking here (HTML-only stubs)
    r"|here\b)"                                  # ...here
    r"|click\s+here\s+to\s+view",               # click here to view
    re.IGNORECASE)


def extract_browser_link(body: str) -> str:
    """Pull the 'View this email in your browser' URL out of a newsletter body."""
    m = _BROWSER_LINK_RE.search(body or "")
    if not m:
        return ""
    tail = body[m.end(): m.end() + 400]
    um = re.search(r"https?://\S+", tail)
    return um.group(0).rstrip(").,]>\"'") if um else ""


def extract_email_posts(messages: list[dict]) -> list[dict]:
    """Pull real newsletter emails out of #mel-intake messages.

    Returns [{ts, subject, sender_name, sender_addr, body, date}], skipping the Google
    forwarding-confirmation plumbing emails.
    """
    out = []
    for m in messages:
        for f in (m.get("files") or []):
            if f.get("filetype") != "email":
                continue
            frm = (f.get("from") or [{}])[0]
            addr = (frm.get("address") or "").lower()
            subject = (f.get("subject") or f.get("title") or "").strip()
            body = f.get("plain_text") or ""
            if addr in SKIP_SENDERS or SKIP_SUBJECT_RE.search(subject):
                continue
            if not body.strip():
                continue
            out.append({
                "ts": m.get("ts"),
                "subject": subject,
                "sender_name": frm.get("name") or "",
                "sender_addr": addr,
                "body": body,
                "browser_link": extract_browser_link(body),
                "date": datetime.fromtimestamp(float(m.get("ts", 0))).strftime("%Y-%m-%d"),
            })
    return out


NEWSLETTER_SYSTEM = (
    "You are a MEL analyst at Ambitious Impact (AIM). You are given a charity/organisation "
    "newsletter (a forwarded email). Produce a high-level overview of what it covers for "
    "AIM's newsletter tracker. Ignore boilerplate (unsubscribe, 'view in browser', social "
    "links). Do not invent anything not in the email.\n\n"
    "Respond with ONLY a JSON object — no prose, no code fences — with exactly these keys:\n"
    '  "organization_name": string or null  (the org the newsletter is from, e.g. "Fish Welfare Initiative")\n'
    '  "whats_covered": string  (2-4 VERY short bullet points, each starting with "- " on its own line; '
    "each a brief phrase under ~10 words, not a full sentence; only the most important items)"
)


def summarise_newsletter(subject: str, sender: str, body: str, api_key: str, model: str) -> dict:
    """Call Claude to extract the structured newsletter fields."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)
    body = body[:14000]  # keep request under tight per-minute token limits
    user_msg = (
        f"Newsletter subject: {subject}\n"
        f"From: {sender}\n\n"
        f"Body:\n{body}\n\n"
        "Return the JSON object now."
    )
    resp = client.messages.create(
        model=model, max_tokens=1500, system=NEWSLETTER_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", candidate, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    out = {
        "organization_name": data.get("organization_name") or None,
        "whats_covered": (data.get("whats_covered") or "").strip(),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Forwarded newsletters in #mel-intake -> Airtable Newsletters.")
    ap.add_argument("--dry-run", action="store_true", help="Summarise and print rows, but don't write to Airtable.")
    ap.add_argument("--since-days", type=int, default=None, help="Lookback window in days (default MEL_NEWSLETTER_LOOKBACK_DAYS or 14).")
    ap.add_argument("--limit", type=int, default=None, help="Override how many channel messages to read.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    mel.load_env_file(mel.ENV_PATH)

    workspace = os.environ.get("SLACK_WORKSPACE", "ceincubationprogram")
    intake_channel = os.environ.get("MEL_INTAKE_CHANNEL", "C0B9WG6UY4W")
    base_id = os.environ.get("AIRTABLE_BASE_ID", "app6tmBJhcfCS7FLs")
    table_id = os.environ.get("AIRTABLE_NEWSLETTERS_TABLE_ID", "tblr8m5vdnya1PmzD")
    charities_table = os.environ.get("AIRTABLE_CHARITIES_TABLE_ID", "tblSsWP0lp1fH9kk6")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    history_limit = args.limit or int(os.environ.get("SLACK_HISTORY_LIMIT", "200"))
    lookback_days = args.since_days if args.since_days is not None else int(os.environ.get("MEL_NEWSLETTER_LOOKBACK_DAYS", "14"))

    mel.require_env("SLACK_USER_TOKEN")
    anthropic_key = mel.require_env("ANTHROPIC_API_KEY")
    airtable_token = os.environ.get("AIRTABLE_API_KEY")
    if not airtable_token and not args.dry_run:
        raise SystemExit("Missing required env var AIRTABLE_API_KEY.")

    # Auth check (fail loudly on a dead token — the Actions run goes red + emails you).
    try:
        auth = mel.slack("auth", workspace=workspace)
        log.info("Slack auth OK as %s.", auth.get("user"))
    except (mel.SlackAuthError, mel.SlackCallError) as e:
        log.error("ALERT: Slack auth failed (%s). Check/refresh SLACK_USER_TOKEN.", e)
        return 1

    since_ts = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    try:
        hist = mel.slack("history", intake_channel, str(history_limit), workspace=workspace)
    except (mel.SlackAuthError, mel.SlackCallError) as e:
        log.error("ALERT: could not read #mel-intake (%s).", e)
        return 1

    messages = [m for m in hist.get("messages", []) if float(m.get("ts", 0)) > since_ts]
    posts = extract_email_posts(messages)
    log.info("Found %d newsletter email(s) in the last %d days.", len(posts), lookback_days)
    if not posts:
        log.info("Done. nothing to import.")
        return 0

    charities = []
    if airtable_token:
        try:
            charities = mel.fetch_charities(airtable_token, base_id, charities_table)
        except Exception as e:
            log.warning("Could not load Charities for linking (%s).", e)

    records, errors = [], 0
    for p in posts:
        sender = f"{p['sender_name']} <{p['sender_addr']}>".strip()
        try:
            s = summarise_newsletter(p["subject"], sender, p["body"], anthropic_key, model)
        except Exception as e:
            log.error("Summarisation failed for %r (%s); skipping this run.", p["subject"], e)
            errors += 1
            continue

        fields = {
            "Subject": p["subject"],
            "Date": p["date"],
            "From": sender,
            "What's covered": s["whats_covered"],
            "Raw email": p["body"][:90000],  # the raw copy (Airtable long-text limit ~100k)
            "Email link": p["browser_link"],
            "Source Msg TS": p["ts"],
            "Slack link": f"https://{workspace}.slack.com/archives/{intake_channel}/p{p['ts'].replace('.', '')}",
        }
        rec_id = mel.match_charity(s.get("organization_name"), None, charities)
        if rec_id:
            fields["Organization"] = [rec_id]
        else:
            log.info("No confident charity match for %r (org=%r); Organization left blank.",
                     p["subject"], s.get("organization_name"))
        records.append(fields)
        if args.dry_run:
            log.info("[dry-run] would upsert newsletter:\n%s",
                     json.dumps(fields, indent=2, ensure_ascii=False))

    if records and not args.dry_run:
        try:
            mel.upsert_records(airtable_token, base_id, table_id, records, merge_field="Source Msg TS")
            log.info("Upserted %d newsletter row(s).", len(records))
        except Exception as e:
            log.error("ALERT: Airtable upsert failed (%s).", e)
            return 1

    log.info("Done. newsletters=%d errors=%d%s",
             len(records), errors, " (dry-run: nothing written)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
