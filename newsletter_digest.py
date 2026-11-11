#!/usr/bin/env python3
"""
Weekly newsletter digest -> #aim-staff and #aim-community.

Reads the past week's rows from the Airtable "Newsletter Intake" table, groups them BY
ORGANISATION, and has Claude Sonnet 4.6 write a few concise bullets of noteworthy updates per
org (hiring, progress, new evidence/evaluations, funding, and anything else notable). Each
channel gets a one-line "Charity newsletter roundup" message with the org sections in its
thread, each org headed by a link to its "view in browser" page so people can read it.

#aim-staff gets every org, falling back to the org's Airtable row when an email has no web
version. #aim-community is shared beyond AIM, so it only gets charities matched to a MEL
Charities record whose "Undercover?" field is blank, links only to public pages, and drops
any section that names an undercover charity. Run `python3 newsletter_digest.py --help`.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import mel_intake as mel  # shared Slack send, Airtable helpers, charity list, env loading

log = logging.getLogger("newsletter_digest")

RAW_PER_ORG = 6000     # chars of raw email per org fed to the model
TOTAL_CAP = 32000      # total chars across orgs (keeps under the token limit)
SLACK_REPLY_MAX = 3800  # Slack shows up to ~4,000 characters a message, so long threads split

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
                                                 "Organization", "Email link"]}
        if offset:
            params["offset"] = offset
        r = requests.get(url, headers=mel.airtable_headers(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for rec in body.get("records", []):
            f = rec.get("fields", {})
            if f.get("Date") and f["Date"] >= cutoff:
                # Fallback link target when the email has no public "view in browser" URL:
                # the Airtable row itself (holds the recovered full copy + "What's covered").
                f["_airtable_url"] = f"https://airtable.com/{base_id}/{table}/{rec['id']}"
                out.append(f)
        offset = body.get("offset")
        if not offset:
            break
    return out


def charity_privacy(token: str, base_id: str, table: str) -> tuple[dict, set]:
    """Return ({MEL Charities record id: name}, {ids of charities that mustn't be named publicly}).

    A charity is private when its "Undercover?" field holds anything but "No" ("Yes",
    "Private research report", ...). The AIM website base mirrors this field by hand.
    """
    url = f"{mel.AIRTABLE_API}/{base_id}/{table}"
    names, private, offset = {}, set(), None
    while True:
        params = {"pageSize": 100, "fields[]": ["Name", "Undercover?"]}
        if offset:
            params["offset"] = offset
        r = requests.get(url, headers=mel.airtable_headers(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for rec in body.get("records", []):
            f = rec.get("fields") or {}
            if f.get("Name"):
                names[rec["id"]] = f["Name"]
            if (f.get("Undercover?") or "").strip().lower() not in ("", "no"):
                private.add(rec["id"])
        offset = body.get("offset")
        if not offset:
            break
    return names, private


def linked_ids(row: dict) -> list[str]:
    """MEL Charities record ids in a row's Organization link."""
    return [o if isinstance(o, str) else (o or {}).get("id") for o in (row.get("Organization") or [])]


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
    """Group rows by org name, keeping each org's charity ids, latest date and newest links."""
    groups: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("Date", "")):  # oldest first, so newest wins
        name = org_label(row, charities_by_id)
        g = groups.setdefault(name, {"rows": [], "date": "", "ids": set(),
                                     "public_link": "", "airtable_link": ""})
        g["rows"].append(row)
        g["ids"].update(i for i in linked_ids(row) if i)
        g["date"] = row.get("Date") or g["date"]
        # The email's public "view in browser" page opens for anyone; the Airtable row (which
        # holds the full copy) opens only for staff.
        g["public_link"] = mel.clean_public_link(row.get("Email link") or "") or g["public_link"]
        g["airtable_link"] = row.get("_airtable_url") or g["airtable_link"]
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


def section(name: str, g: dict, bullets: list, link: str) -> str:
    """One org's block for the thread: its name (linked when there's a link) and its bullets."""
    head = f"*<{link}|{name}>*" if link else f"*{name}*"
    if len(g["rows"]) > 1:
        head += f"  _({len(g['rows'])} emails)_"
    if bullets:
        return head + "\n" + "\n".join(f"• {b}" for b in bullets)
    # No bullets. If it's a content-less HTML-only stub (not a regranter with only excluded
    # grantee news), point to the email instead of dropping it silently.
    raw_all = "\n\n".join((r.get("Raw email") or "") for r in g["rows"])
    if mel.is_contentless(raw_all) and link:
        subj = max(g["rows"], key=lambda r: r.get("Date", "")).get("Subject", "").strip()
        return head + "\n" + (f"• _{subj}_ — HTML-only email, open to read." if subj
                              else "• _HTML-only email — open to read._")
    return ""


def replies(sections: list[str], limit: int = SLACK_REPLY_MAX) -> list[str]:
    """Pack the org sections into as few thread replies as fit under `limit` characters."""
    out, cur = [], ""
    for s in sections:
        if cur and len(cur) + 2 + len(s) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{s}" if cur else s
    if cur:
        out.append(cur)
    return out


def send(channel: str, text: str, thread_ts: str = "", workspace: str | None = None) -> str:
    """Post one message and return its ts. (mel.slack returns Slack's errors rather than raising.)"""
    j = mel.slack("send", channel, text, thread_ts, workspace=workspace)
    if not j.get("ok"):
        raise mel.SlackCallError(f"chat.postMessage to {channel} failed: {j.get('error')}")
    return j["ts"]


def post_roundup(channel: str, header: str, sections: list[str], workspace: str) -> None:
    """One short line in the channel; the org sections go in its thread."""
    parent = send(channel, header, workspace=workspace)
    for text in replies(sections):
        time.sleep(1)  # Slack allows about one message a second per channel
        send(channel, text, parent, workspace=workspace)


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
    # #aim-community: shared with ~530 people beyond AIM. Set to "" to stop posting there.
    community_channel = os.environ.get("MEL_COMMUNITY_CHANNEL", "C01CB2X08Q5")
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

    charities_by_id, private_ids = charity_privacy(airtable_token, base_id, charities_table)
    private_names = [charities_by_id[i].lower() for i in private_ids if i in charities_by_id]
    groups = group_by_org(rows, charities_by_id)
    bullets = org_bullets(groups, anthropic_key, model)
    bull_by_norm = {_norm(k): v for k, v in bullets.items()}

    staff, community, kept_out = [], [], []
    for name, g in sorted(groups.items(), key=lambda kv: kv[1]["date"], reverse=True):
        bl = bull_by_norm.get(_norm(name)) or []
        s = section(name, g, bl, g["public_link"] or g["airtable_link"])
        if not s:
            continue
        staff.append(s)
        # #aim-community reaches people outside AIM: only charities matched to a MEL record
        # and not undercover, linked only to public pages, and never naming an undercover one.
        public = bool(g["ids"]) and not (g["ids"] & private_ids)
        c = section(name, g, bl, g["public_link"]) if public else ""
        if c and not any(n in c.lower() for n in private_names):
            community.append(c)
        else:
            kept_out.append(name)

    if not staff:
        log.warning("Nothing notable to post.")
        return 0

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%-d %b")
    end = datetime.now(timezone.utc).strftime("%-d %b")
    header = f"🤖 *Charity newsletter roundup for {start}–{end}*"
    targets = [(channel, staff)]
    if community_channel and community:
        targets.append((community_channel, community))
    if community_channel and kept_out:
        log.info("Kept out of #aim-community (undercover, not matched to a charity, or no "
                 "public link for an HTML-only email): %s", ", ".join(kept_out))

    if args.dry_run:
        for ch, secs in targets:
            log.info("[dry-run] would post to %s:\n%s\n\n[in its thread]\n%s", ch, header,
                     "\n\n[next reply]\n".join(replies(secs)))
        return 0

    failed = False
    for ch, secs in targets:
        try:
            post_roundup(ch, header, secs, workspace)
            log.info("Posted the roundup to %s with %d org(s) in its thread.", ch, len(secs))
        except (mel.SlackAuthError, mel.SlackCallError) as e:
            log.error("ALERT: failed to post the roundup to %s (%s).", ch, e)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
