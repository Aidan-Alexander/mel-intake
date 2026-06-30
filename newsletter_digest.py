#!/usr/bin/env python3
"""
Weekly newsletter digest -> #aim-staff.

Reads the past week's rows from the Airtable "Newsletter Intake" table (populated by
newsletter_intake.py), has Claude Sonnet 4.6 group the noteworthy updates into
Hiring / Progress / New evidence & evaluations / Funding, and posts the digest to
#aim-staff. Run `python3 newsletter_digest.py --help`.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import mel_intake as mel  # shared Slack send, Airtable headers, env loading

log = logging.getLogger("newsletter_digest")

RAW_PER_NEWSLETTER = 4000   # chars of each raw email fed to the model
TOTAL_CAP = 32000           # total chars across newsletters (keeps under the token limit)

DIGEST_SYSTEM = (
    "You are compiling a weekly internal digest for Ambitious Impact (AIM) staff from "
    "charity newsletters received this week. Group the noteworthy updates into these "
    "sections, in this order, and INCLUDE ONLY sections that actually have content:\n"
    "  *Hiring* — open roles, key hires, departures\n"
    "  *Progress* — geographic expansion, new partnerships, new product/programme features, "
    "major operational milestones\n"
    "  *New evidence & evaluations* — studies, evaluations, reviews, or results published\n"
    "  *Funding* — funding received, raised, or granted\n\n"
    "Under each section use '• ' bullets, each a short line attributed to the org, e.g. "
    "'• Fish Welfare Initiative: hiring a Director of Programs'. Keep it tight and scannable "
    "for busy staff; omit fluff, events, and minor news. Use Slack mrkdwn ('*Section*' bold "
    "headers, '• ' bullets). Do not invent anything — use only what's in the newsletters. "
    "Output ONLY the digest body (the sections), no preamble or sign-off."
)


def fetch_recent(token: str, base_id: str, table: str, days: int) -> list[dict]:
    """Fetch Newsletter Intake rows whose Date is within the last `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out, offset = [], None
    url = f"{mel.AIRTABLE_API}/{base_id}/{table}"
    while True:
        params = {"pageSize": 100, "fields[]": ["Subject", "From", "Date", "Raw email"]}
        if offset:
            params["offset"] = offset
        r = requests.get(url, headers=mel.airtable_headers(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for rec in body.get("records", []):
            f = rec.get("fields", {})
            if f.get("Date") and f["Date"] >= cutoff:
                out.append(f)
        offset = body.get("offset")
        if not offset:
            break
    out.sort(key=lambda f: f.get("Date", ""))
    return out


def build_digest_body(items: list[dict], api_key: str, model: str) -> str:
    """Ask Claude to turn the week's newsletters into the themed digest body."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)
    blocks, total = [], 0
    for it in items:
        raw = (it.get("Raw email") or "")[:RAW_PER_NEWSLETTER]
        block = (f"From: {it.get('From', '')}\nSubject: {it.get('Subject', '')}\n"
                 f"Date: {it.get('Date', '')}\n{raw}")
        if total + len(block) > TOTAL_CAP:
            break
        blocks.append(block)
        total += len(block)
    user_msg = "\n\n=====\n\n".join(blocks) + "\n\nProduce the digest now."
    resp = client.messages.create(
        model=model, max_tokens=2000, system=DIGEST_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly newsletter digest -> #aim-staff.")
    ap.add_argument("--dry-run", action="store_true", help="Build and print the digest, but don't post to Slack.")
    ap.add_argument("--since-days", type=int, default=None, help="Lookback window in days (default MEL_DIGEST_LOOKBACK_DAYS or 7).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    mel.load_env_file(mel.ENV_PATH)

    workspace = os.environ.get("SLACK_WORKSPACE", "ceincubationprogram")
    base_id = os.environ.get("AIRTABLE_BASE_ID", "app6tmBJhcfCS7FLs")
    table_id = os.environ.get("AIRTABLE_NEWSLETTERS_TABLE_ID", "tblr8m5vdnya1PmzD")
    channel = os.environ.get("MEL_DIGEST_CHANNEL", "CMS6V5XEJ")  # #aim-staff
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    days = args.since_days if args.since_days is not None else int(os.environ.get("MEL_DIGEST_LOOKBACK_DAYS", "7"))

    mel.require_env("SLACK_USER_TOKEN")
    anthropic_key = mel.require_env("ANTHROPIC_API_KEY")
    airtable_token = mel.require_env("AIRTABLE_API_KEY")

    try:
        mel.slack("auth", workspace=workspace)
    except (mel.SlackAuthError, mel.SlackCallError) as e:
        log.error("ALERT: Slack auth failed (%s). Check/refresh SLACK_USER_TOKEN.", e)
        return 1

    items = fetch_recent(airtable_token, base_id, table_id, days)
    log.info("%d newsletter(s) in the last %d days.", len(items), days)
    if not items:
        log.info("No newsletters this week; nothing to post.")
        return 0

    body = build_digest_body(items, anthropic_key, model)
    if not body:
        log.warning("Empty digest body; not posting.")
        return 0

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%-d %b")
    end = datetime.now(timezone.utc).strftime("%-d %b")
    header = f"*Newsletter digest — {start}–{end}*  ({len(items)} newsletter{'s' if len(items) != 1 else ''})\n\n"
    message = header + body

    if args.dry_run:
        log.info("[dry-run] would post to %s:\n\n%s", channel, message)
        return 0

    try:
        mel.slack("send", channel, message, workspace=workspace)
        log.info("Posted weekly digest to %s.", channel)
    except (mel.SlackAuthError, mel.SlackCallError) as e:
        log.error("ALERT: failed to post digest (%s).", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
