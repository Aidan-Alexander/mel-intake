#!/usr/bin/env python3
"""
Quarterly coverage check: which active charities have we received NO newsletter from?

Compares the active charities (Charities table, Status = "Active") against the newsletters
captured in the Airtable "Newsletter Intake" table over the last quarter, and DMs Morgan
Fairless the list of active charities with zero newsletters — so we can check we're
subscribed to their mailing lists. Run `python3 charity_newsletter_check.py --help`.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import mel_intake as mel  # shared Slack send, Airtable headers, env loading

log = logging.getLogger("charity_newsletter_check")


def fetch_active_charities(token: str, base_id: str, table: str) -> list[dict]:
    """Return [{id, name}] for charities with Status == 'Active'."""
    out, offset = [], None
    url = f"{mel.AIRTABLE_API}/{base_id}/{table}"
    while True:
        params = {"pageSize": 100, "fields[]": ["Name", "Status"]}
        if offset:
            params["offset"] = offset
        b = requests.get(url, headers=mel.airtable_headers(token), params=params, timeout=30).json()
        for rec in b.get("records", []):
            f = rec.get("fields", {})
            status = f.get("Status")
            status = status.get("name") if isinstance(status, dict) else status
            if status == "Active" and f.get("Name"):
                out.append({"id": rec["id"], "name": f["Name"]})
        offset = b.get("offset")
        if not offset:
            break
    return out


def orgs_with_newsletters(token: str, base_id: str, table: str, days: int) -> set:
    """Set of Charity record IDs linked from a newsletter in the last `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    seen, offset = set(), None
    url = f"{mel.AIRTABLE_API}/{base_id}/{table}"
    while True:
        params = {"pageSize": 100, "fields[]": ["Organization", "Date"]}
        if offset:
            params["offset"] = offset
        b = requests.get(url, headers=mel.airtable_headers(token), params=params, timeout=30).json()
        for rec in b.get("records", []):
            f = rec.get("fields", {})
            if f.get("Date") and f["Date"] >= cutoff:
                for o in (f.get("Organization") or []):
                    seen.add(o if isinstance(o, str) else o.get("id"))
        offset = b.get("offset")
        if not offset:
            break
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description="Quarterly: DM Morgan the active charities with no newsletter this quarter.")
    ap.add_argument("--dry-run", action="store_true", help="Print the list instead of DMing Morgan.")
    ap.add_argument("--since-days", type=int, default=None, help="Lookback window (default MEL_COVERAGE_LOOKBACK_DAYS or 90).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    mel.load_env_file(mel.ENV_PATH)

    workspace = os.environ.get("SLACK_WORKSPACE", "ceincubationprogram")
    base_id = os.environ.get("AIRTABLE_BASE_ID", "app6tmBJhcfCS7FLs")
    newsletters_table = os.environ.get("AIRTABLE_NEWSLETTERS_TABLE_ID", "tblr8m5vdnya1PmzD")
    charities_table = os.environ.get("AIRTABLE_CHARITIES_TABLE_ID", "tblSsWP0lp1fH9kk6")
    morgan = os.environ.get("MEL_MORGAN_USER_ID", "U0479N9QX5W")  # Morgan Fairless
    days = args.since_days if args.since_days is not None else int(os.environ.get("MEL_COVERAGE_LOOKBACK_DAYS", "90"))

    mel.require_env("SLACK_USER_TOKEN")
    airtable_token = mel.require_env("AIRTABLE_API_KEY")

    try:
        mel.slack("auth", workspace=workspace)
    except (mel.SlackAuthError, mel.SlackCallError) as e:
        log.error("ALERT: Slack auth failed (%s). Check/refresh SLACK_USER_TOKEN.", e)
        return 1

    active = fetch_active_charities(airtable_token, base_id, charities_table)
    seen = orgs_with_newsletters(airtable_token, base_id, newsletters_table, days)
    missing = sorted(c["name"] for c in active if c["id"] not in seen)
    log.info("%d active charities; %d with a newsletter in last %dd; %d with none.",
             len(active), len(seen), days, len(missing))

    if not missing:
        log.info("Every active charity sent a newsletter this quarter; nothing to flag.")
        return 0

    bullets = "\n".join(f"• {n}" for n in missing)
    message = (
        "🤖 *Quarterly newsletter coverage check*\n\n"
        f"These {len(missing)} active charities haven't sent any newsletter to #mel-intake in "
        f"the last {days} days. Can we check we're subscribed to their mailing lists (and that "
        f"forwarding to #mel-intake is set up)?\n\n{bullets}"
    )

    if args.dry_run:
        log.info("[dry-run] would DM %s:\n\n%s", morgan, message)
        return 0

    try:
        mel.slack("send", morgan, message, workspace=workspace)
        log.info("DMed coverage check to Morgan (%s): %d charities flagged.", morgan, len(missing))
    except (mel.SlackAuthError, mel.SlackCallError) as e:
        log.error("ALERT: failed to DM Morgan (%s).", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
