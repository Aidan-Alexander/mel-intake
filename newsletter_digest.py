#!/usr/bin/env python3
"""
Weekly newsletter digest -> #aim-staff.

Reads the past week's rows from the Airtable "Newsletter Intake" table, groups them BY
ORGANISATION, has Claude Sonnet 4.6 write a few concise bullets of noteworthy updates per
org (hiring, progress, new evidence/evaluations, funding, and anything else notable), and
posts the digest to #aim-staff — each org headed by a link to its "View this email in your
browser" page so people can read it properly. Run `python3 newsletter_digest.py --help`.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import mel_intake as mel  # shared Slack send, Airtable helpers, charity list, env loading

log = logging.getLogger("newsletter_digest")

RAW_PER_ORG = 6000     # chars of raw email per org fed to the model
TOTAL_CAP = 32000      # total chars across orgs (keeps under the token limit)

DIGEST_SYSTEM = (
    "You are compiling a weekly internal digest for Ambitious Impact (AIM) staff from "
    "charity newsletters received this week, grouped by organisation. For EACH organisation "
    "block below, write 2-5 short bullet points covering the noteworthy updates — e.g. new "
    "hires/roles or departures; progress (geographic expansion, new partnerships, new "
    "product/programme features, milestones); new evidence or evaluations; funding received; "
    "and any other genuinely notable news. Keep bullets short and scannable, keep key "
    "numbers, omit fluff/events/minor news, and do not invent anything.\n\n"
    "CRITICAL — each section must be about THE ORGANISATION ITSELF only. Include its own "
    "direct actions: hires/departures, strategy, products/programmes it runs, partnerships it "
    "forms, research/evaluations IT publishes, funding IT receives, and content IT produces "
    "(blog series, reports, tools, surveys). EXCLUDE the achievements, milestones, legislative "
    "wins, launches, or results of the charities/grantees/partners it recommends, funds, or "
    "regrants to — EVEN when the newsletter presents them as 'highlights', 'wins', or outcomes "
    "of its support. Example to omit: a climate regranter reporting that a recommended charity "
    "helped pass a law, or that a grantee launched a consortium — that is NOT an update about "
    "the regranter. If, after excluding such third-party news, an organisation has nothing "
    "notable about itself, return an empty array for it (its section is then dropped).\n\n"
    "Return ONLY a JSON object mapping each organisation's exact name (the value after "
    "'ORG:') to an array of bullet strings (plain text, no leading bullet character)."
)


def fetch_recent(token: str, base_id: str, table: str, days: int) -> list[dict]:
    """Fetch Newsletter Intake rows whose Date is within the last `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out, offset = [], None
    url = f"{mel.AIRTABLE_API}/{base_id}/{table}"
    while True:
        params = {"pageSize": 100, "fields[]": ["Subject", "From", "Date", "Raw email",
                                                 "Organization", "Email link", "Slack link"]}
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
    return out


def org_label(row: dict, charities_by_id: dict) -> str:
    """Org name from the linked Charities record, falling back to the From display name."""
    org = row.get("Organization") or []
    if org:
        rid = org[0] if isinstance(org[0], str) else (org[0] or {}).get("id")
        if rid and charities_by_id.get(rid):
            return charities_by_id[rid]
    name = (row.get("From") or "").split("<")[0].strip()
    if "|" in name:
        name = name.split("|")[-1].strip()
    return name or "Unknown"


def group_by_org(rows: list[dict], charities_by_id: dict) -> dict:
    """Group rows by org name, tracking the most recent date and its link per org."""
    groups: dict[str, dict] = {}
    for row in rows:
        name = org_label(row, charities_by_id)
        g = groups.setdefault(name, {"rows": [], "date": "", "link": ""})
        g["rows"].append(row)
        d = row.get("Date", "")
        if d >= g["date"]:  # prefer the most recent newsletter's link
            g["date"] = d
            g["link"] = row.get("Email link") or row.get("Slack link") or g["link"]
    return groups


def org_bullets(groups: dict, api_key: str, model: str) -> dict:
    """Ask Claude for {org_name: [bullets]} across all orgs in one call."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)
    blocks, total = [], 0
    for name, g in groups.items():
        raw = "\n\n".join((r.get("Raw email") or "") for r in g["rows"])[:RAW_PER_ORG]
        block = f"ORG: {name}\n{raw}"
        if total + len(block) > TOTAL_CAP:
            break
        blocks.append(block)
        total += len(block)
    user_msg = "\n\n=====\n\n".join(blocks) + "\n\nReturn the JSON now."
    resp = client.messages.create(model=model, max_tokens=2000, system=DIGEST_SYSTEM,
                                  messages=[{"role": "user", "content": user_msg}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else {}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def is_contentless(raw: str) -> bool:
    """True if the plain-text body is just an HTML-only placeholder with no real content.

    HTML-only mailers (MailerLite, some Mailchimp setups) send a plain-text part that is
    only a "your email can't display HTML — view in browser" stub. Slack's email-to-channel
    integration forwards just that part, so there's nothing to summarise and the model
    returns no bullets. We detect these by their tiny prose length (URLs stripped) rather
    than dropping them like a regranter whose only news is excluded grantee wins (those
    bodies are long).
    """
    prose = re.sub(r"https?://\S+", "", raw or "")   # drop the tracking/view URLs
    prose = re.sub(r"\s+", " ", prose).strip()
    return len(prose) < 600


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly newsletter digest (by org) -> #aim-staff.")
    ap.add_argument("--dry-run", action="store_true", help="Build and print the digest, but don't post.")
    ap.add_argument("--since-days", type=int, default=None, help="Lookback window in days (default MEL_DIGEST_LOOKBACK_DAYS or 7).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    mel.load_env_file(mel.ENV_PATH)

    workspace = os.environ.get("SLACK_WORKSPACE", "ceincubationprogram")
    base_id = os.environ.get("AIRTABLE_BASE_ID", "app6tmBJhcfCS7FLs")
    table_id = os.environ.get("AIRTABLE_NEWSLETTERS_TABLE_ID", "tblr8m5vdnya1PmzD")
    charities_table = os.environ.get("AIRTABLE_CHARITIES_TABLE_ID", "tblSsWP0lp1fH9kk6")
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

    rows = fetch_recent(airtable_token, base_id, table_id, days)
    log.info("%d newsletter(s) in the last %d days.", len(rows), days)
    if not rows:
        log.info("No newsletters this week; nothing to post.")
        return 0

    charities = mel.fetch_charities(airtable_token, base_id, charities_table)
    charities_by_id = {c["id"]: c["name"] for c in charities}
    groups = group_by_org(rows, charities_by_id)
    bullets = org_bullets(groups, anthropic_key, model)
    bull_by_norm = {_norm(k): v for k, v in bullets.items()}

    sections = []
    for name, g in sorted(groups.items(), key=lambda kv: kv[1]["date"], reverse=True):
        bl = bull_by_norm.get(_norm(name)) or []
        head = f"*<{g['link']}|{name}>*" if g["link"] else f"*{name}*"
        if len(g["rows"]) > 1:
            head += f"  _({len(g['rows'])} emails)_"
        if not bl:
            # No bullets. If it's a content-less HTML-only stub (not a regranter with only
            # excluded grantee news), surface it with a link instead of dropping it silently.
            raw_all = "\n\n".join((r.get("Raw email") or "") for r in g["rows"])
            if is_contentless(raw_all) and g["link"]:
                subj = max(g["rows"], key=lambda r: r.get("Date", "")).get("Subject", "").strip()
                note = f"• _{subj}_ — HTML-only email, open to read." if subj \
                    else "• _HTML-only email — open to read._"
                sections.append(head + "\n" + note)
            continue
        sections.append(head + "\n" + "\n".join(f"• {b}" for b in bl))

    if not sections:
        log.warning("Nothing notable to post.")
        return 0

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%-d %b")
    end = datetime.now(timezone.utc).strftime("%-d %b")
    header = f"🤖 *Charity newsletter roundup for {start}–{end}*\n\n"
    message = header + "\n\n".join(sections)

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
