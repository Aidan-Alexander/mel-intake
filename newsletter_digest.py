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
any section that names an undercover charity.

Every email is either linked or attached (Aidan, Nov 2026): one with a web version is
linked; one without is attached to the thread as a PDF of the full email (rendered with
headless Chrome, cleaned of AIM's unsubscribe links and address); and any report an email
came with is attached too. Uploading needs the Slack app's files:write scope. In
#aim-community a file is attached only if its text could be read and names no undercover
charity. Run `python3 newsletter_digest.py --help`.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path

import requests
import mel_intake as mel  # shared Slack send, Airtable helpers, charity list, env loading

log = logging.getLogger("newsletter_digest")

RAW_PER_ORG = 6000     # chars of raw email per org fed to the model
TOTAL_CAP = 32000      # total chars across orgs (keeps under the token limit)
SLACK_REPLY_MAX = 3800  # Slack shows up to ~4,000 characters a message, so long threads split
REPORT_EXTS = (".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls")  # attachments passed on

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
                                                 "Organization", "Email link", "Source Msg TS"]}
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
    """Ask Claude for {org_name: [bullets]} across all orgs in one call.

    Every org gets an equal share of TOTAL_CAP (at most RAW_PER_ORG), so none is left out.
    Until Nov 2026 orgs past the cap were silently dropped: a five-week test lost 3 of 10.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)
    per_org = min(RAW_PER_ORG, TOTAL_CAP // max(1, len(groups)) - 100)
    blocks = []
    for name, g in groups.items():
        raw = "\n\n".join((r.get("Raw email") or "") for r in g["rows"])[:per_org]
        blocks.append(f"ORG: {name}\n{raw}")
    user_msg = "\n\n=====\n\n".join(blocks) + "\n\nReturn the JSON now."
    resp = client.messages.create(model=model, max_tokens=4000, system=DIGEST_SYSTEM,
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


def link_text(s: str) -> str:
    return re.sub(r"[<>|]", " ", s or "").strip()


def other_links(g: dict, heading_link: str) -> list[tuple[str, str]]:
    """(subject, web version) of the org's other emails, so every email that has one is linked."""
    seen, out = {heading_link}, []
    for row in sorted(g["rows"], key=lambda r: r.get("Date", ""), reverse=True):
        url = mel.clean_public_link(row.get("Email link") or "")
        if url and url not in seen:
            seen.add(url)
            out.append((row.get("Subject") or "another email", url))
    return out


def section(name: str, g: dict, bullets: list, link: str, others: list = (), note: str = "") -> str:
    """One org's block for the thread: its name (linked when there's a link, followed by `note`
    when its files are attached at the end of the thread) and its bullets."""
    head = f"*<{link}|{name}>*" if link else f"*{name}*"
    if note:
        head += f" {note}"
    if len(g["rows"]) > 1:
        head += f"  _({len(g['rows'])} emails)_"
    if bullets:
        text = head + "\n" + "\n".join(f"• {b}" for b in bullets)
        if others:
            text += "\n_Also:_ " + ", ".join(f"<{u}|{link_text(s)}>" for s, u in others)
        return text
    # No bullets. If it's a content-less HTML-only stub (not a regranter with only excluded
    # grantee news), point to the email instead of dropping it silently.
    raw_all = "\n\n".join((r.get("Raw email") or "") for r in g["rows"])
    if mel.is_contentless(raw_all) and (link or note):
        subj = max(g["rows"], key=lambda r: r.get("Date", "")).get("Subject", "").strip()
        return head + "\n" + (f"• _{subj}_ — HTML-only email, open to read." if subj
                              else "• _HTML-only email — open to read._")
    return ""


def replies(sections: list[tuple[str, str]], limit: int = SLACK_REPLY_MAX) -> list[tuple[str, list[str]]]:
    """Pack (org, text) sections into as few thread replies as fit under `limit` characters.
    Returns (reply text, orgs in it) pairs."""
    out, cur, names = [], "", []
    for name, s in sections:
        if cur and len(cur) + 2 + len(s) > limit:
            out.append((cur, names))
            cur, names = "", []
        cur = f"{cur}\n\n{s}" if cur else s
        names.append(name)
    if cur:
        out.append((cur, names))
    return out


# --- Full emails and the reports they came with ----------------------------------------------

SUBSCRIBER_LINK = re.compile(
    r"unsubscribe|opt.?out|preferences|update (your )?(profile|details)|manage (your )?subscription|"
    r"forward (this|to a friend)|why did i get|abmelden|disiscriviti|cancella|cancelar|"
    r"darse de baja|desuscrib|suscripci[oó]n|d[ée]sinscri|d[ée]sabonn", re.I)
AIM_ADDRESS = re.compile(r"[\w.+-]+@(charityentrepreneurship\.com|ambitiousimpact\.com|[\w-]+\.slack\.com)",
                         re.I)


def sanitise_email(html: str) -> str:
    """Strip what's tied to AIM's own subscription before an email is shared: unsubscribe,
    preference and forward links, open-tracking pixels, and AIM or Slack addresses."""
    html = re.sub(r"<a\b[^>]*>.*?</a>",
                  lambda m: "" if SUBSCRIBER_LINK.search(m.group(0)) else m.group(0), html, flags=re.I | re.S)
    tiny = re.compile(r'\b(width|height)\s*=\s*["\']?[0-2](px)?["\'\s/>]|(width|height)\s*:\s*[0-2]px', re.I)
    html = re.sub(r"<img\b[^>]*>", lambda m: "" if tiny.search(m.group(0)) else m.group(0), html, flags=re.I)
    return AIM_ADDRESS.sub("", html)


def visible_text(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", html)))


def chrome_binary() -> str | None:
    for c in (os.environ.get("CHROME_BIN"), shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
              shutil.which("chromium"), shutil.which("chromium-browser"),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if c and os.path.exists(c):
            return c
    return None


def html_to_pdf(html: str) -> bytes:
    """Print HTML to PDF with headless Chrome (preinstalled on GitHub's Ubuntu runners)."""
    chrome = chrome_binary()
    if not chrome:
        raise RuntimeError("Chrome not found (set CHROME_BIN)")
    with tempfile.TemporaryDirectory() as tmp:
        src, pdf = Path(tmp) / "email.html", Path(tmp) / "email.pdf"
        src.write_text(html, encoding="utf-8")
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
                        f"--user-data-dir={Path(tmp) / 'profile'}", "--virtual-time-budget=10000",
                        f"--print-to-pdf={pdf}", src.as_uri()], check=True, capture_output=True, timeout=180)
        return pdf.read_bytes()


def slack_email(channel: str, ts: str) -> dict | None:
    """The forwarded email's Slack file (its HTML and attachments) for a Newsletter Intake row."""
    j = mel._slack_post("conversations.history", {"channel": channel, "oldest": ts, "latest": ts,
                                                  "inclusive": "true", "limit": "1"})
    for m in j.get("messages") or []:
        if m.get("ts") == ts:
            return next((f for f in m.get("files") or [] if f.get("filetype") == "email"), None)
    return None


def email_pdf(email: dict, row: dict) -> tuple[bytes, str]:
    """A cleaned PDF of a forwarded email with a one-line header. Returns (pdf, visible text)."""
    raw = mel.download_slack_file(email.get("url_private_download") or email.get("url_private"),
                                  max_bytes=10_000_000, allow_html=True)
    if not raw:
        raise RuntimeError("couldn't download the email from Slack")
    html = sanitise_email(raw.decode("utf-8", "replace"))
    sender = (row.get("From") or "").split("<")[0].strip() or "the sender"
    try:
        when = datetime.strptime(row.get("Date", ""), "%Y-%m-%d").strftime("%-d %B %Y")
    except ValueError:
        when = row.get("Date", "")
    banner = ('<div style="font:13px/1.4 -apple-system,Helvetica,Arial,sans-serif;color:#555;'
              'border-bottom:1px solid #ddd;padding:6px 0 8px;margin:0 0 12px">'
              f'Newsletter from <b>{escape(sender)}</b>, received {escape(when)}: '
              f'<i>{escape(row.get("Subject") or "")}</i></div>')
    style = "<style>@page{margin:12mm}</style>"
    html = (re.sub(r"<body\b[^>]*>", lambda m: m.group(0) + banner, html, count=1, flags=re.I)
            if re.search(r"<body\b", html, re.I) else banner + html)
    html = (re.sub(r"<head\b[^>]*>", lambda m: m.group(0) + style, html, count=1, flags=re.I)
            if re.search(r"<head\b", html, re.I) else style + html)
    return html_to_pdf(html), visible_text(html)


def file_name(org: str, subject: str) -> str:
    """The subject, prefixed with the org's name unless the subject already contains it."""
    short = re.sub(r"\s*\(.*\)$", "", org).lower()  # "Lead Exposure ... (LEEP)" -> "lead exposure ..."
    title = subject if short and short in subject.lower() else f"{org} - {subject}"
    base = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", title)).strip()
    return base[:90].rstrip(" .-") + ".pdf"


def org_files(name: str, g: dict, intake_channel: str, problems: list) -> list[dict]:
    """Files to put under an org: a PDF of each email that has no web version, and every report
    the emails came with. Each is {name, data, text (None if unreadable), kind}."""
    out = []
    for row in g["rows"]:
        needs_pdf = not mel.clean_public_link(row.get("Email link") or "")
        ts = row.get("Source Msg TS") or ""
        email = slack_email(intake_channel, ts) if ts else None
        if not email:
            if needs_pdf:
                problems.append(f"{name}: couldn't find {row.get('Subject')!r} in #mel-intake")
            continue
        if needs_pdf:
            try:
                data, text = email_pdf(email, row)
                out.append({"name": file_name(name, row.get("Subject") or "email"), "data": data,
                            "text": text, "kind": "email"})
            except Exception as e:
                problems.append(f"{name}: couldn't make a PDF of {row.get('Subject')!r} ({e})")
        for a in email.get("attachments") or []:
            fn = a.get("filename") or ""
            if not fn.lower().endswith(REPORT_EXTS) or not a.get("url"):
                continue
            data = mel.download_slack_file(a["url"], max_bytes=50_000_000)
            if not data:
                problems.append(f"{name}: couldn't download the attached {fn!r}")
                continue
            text = mel.pdf_to_text(data, 500_000) if fn.lower().endswith(".pdf") else ""
            out.append({"name": fn, "data": data, "text": text or None, "kind": "report"})
    return out


def attachment_note(files: list[dict]) -> str:
    """What goes after an org's name when its files are attached at the end of the thread."""
    if not files:
        return ""
    many = len(files) > 1
    if any(f["kind"] == "email" for f in files):
        return "(see attached PDFs below)" if many else "(see attached PDF below)"
    return "(see attached reports below)" if many else "(see attached report below)"


AIRTABLE_LINK = re.compile(r"<https?://(?:www\.)?airtable\.com/[^|>]*\|([^>]*)>")


def files_comment(name: str, files: list[dict]) -> str:
    emails = sum(f["kind"] == "email" for f in files)
    reports = len(files) - emails
    bits = []
    if emails:
        bits.append("full email" + ("s" if emails > 1 else "") + " (no web version)")
    if reports:
        bits.append(("its " if emails else "") + "attached report" + ("s" if reports > 1 else ""))
    return f"📎 *{name}*: " + " and ".join(bits)


def community_safe(files: list[dict], private_names: list[str]) -> list[dict]:
    """Only files whose text could be read and names no undercover charity."""
    return [f for f in files if f["text"] and not any(n in f["text"].lower() for n in private_names)]


def send(channel: str, text: str, thread_ts: str = "", workspace: str | None = None) -> str:
    """Post one message and return its ts. (mel.slack returns Slack's errors rather than raising.)"""
    j = mel.slack("send", channel, text, thread_ts, workspace=workspace)
    if not j.get("ok"):
        raise mel.SlackCallError(f"chat.postMessage to {channel} failed: {j.get('error')}")
    return j["ts"]


def post_roundup(channel: str, header: str, sections: list[tuple[str, str]], files_for,
                 workspace: str) -> list[str]:
    """One short line in the channel; the org sections go in its thread, then all the files at
    the end, one message per org in the same order as the sections (Aidan, Nov 2026).
    Returns problems with files (a failed text post raises)."""
    problems = []
    parent = send(channel, header, workspace=workspace)
    for text, _ in replies(sections):
        time.sleep(1)  # Slack allows about one message a second per channel
        send(channel, text, parent, workspace=workspace)
    for name, _ in sections:
        files = files_for(name)
        if not files:
            continue
        time.sleep(1)
        try:
            mel.slack_upload(channel, [(f["name"], f["data"]) for f in files], thread_ts=parent,
                             comment=files_comment(name, files))
        except mel.SlackCallError as e:
            problems.append(f"{name} in {channel}: {e}")
    return problems


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

    # Files (PDFs of emails with no web version, attached reports) are built once per org and
    # shared by both channels; #aim-community only gets the ones whose text could be read and
    # names no undercover charity. They go at the end of the thread, and each section says so.
    intake_channel = os.environ.get("MEL_INTAKE_CHANNEL", "C0B9WG6UY4W")
    file_problems, cache = [], {}

    def staff_files(name: str) -> list[dict]:
        if name not in cache:
            cache[name] = org_files(name, groups[name], intake_channel, file_problems)
        return cache[name]

    def community_files(name: str) -> list[dict]:
        return community_safe(staff_files(name), private_names)

    staff, community, kept_out = [], [], []
    for name, g in sorted(groups.items(), key=lambda kv: kv[1]["date"], reverse=True):
        bl = bull_by_norm.get(_norm(name)) or []
        raw_all = "\n\n".join((r.get("Raw email") or "") for r in g["rows"])
        if not bl and not mel.is_contentless(raw_all):
            log.info("No updates of its own from %s; left out of the roundup.", name)
            continue
        others = other_links(g, g["public_link"])
        s = section(name, g, bl, g["public_link"] or g["airtable_link"], others,
                    attachment_note(staff_files(name)))
        if not s:
            continue
        staff.append((name, s))
        # #aim-community reaches people outside AIM: only charities matched to a MEL record
        # and not undercover, linked only to public pages, and never naming an undercover one.
        public = bool(g["ids"]) and not (g["ids"] & private_ids)
        c = ""
        if public:
            c = section(name, g, bl, g["public_link"], others, attachment_note(community_files(name)))
            c = AIRTABLE_LINK.sub(r"\1", c)  # never an Airtable link here: members can't open it
        if c and not any(n in c.lower() for n in private_names):
            community.append((name, c))
        else:
            kept_out.append(name)

    if not staff:
        log.warning("Nothing notable to post.")
        return 0

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%-d %b")
    end = datetime.now(timezone.utc).strftime("%-d %b")
    header = f"🤖 *Charity newsletter roundup for {start}–{end}*"

    targets = [(channel, staff, staff_files)]
    if community_channel and community:
        targets.append((community_channel, community, community_files))
    if community_channel and kept_out:
        log.info("Kept out of #aim-community (undercover, not matched to a charity, or no "
                 "public link for an HTML-only email): %s", ", ".join(kept_out))

    if args.dry_run:
        save = os.environ.get("MEL_DIGEST_SAVE_DIR")  # optional: keep the files to look at
        for ch, secs, files_for in targets:
            log.info("[dry-run] would post to %s:\n%s\n\n[in its thread]\n%s", ch, header,
                     "\n\n[next reply]\n".join(t for t, _ in replies(secs)))
            for name, _ in secs:
                files = files_for(name)
                if files:
                    log.info("[dry-run] then attach in %s: %s | %s", ch, files_comment(name, files),
                             ", ".join(f"{f['name']} ({len(f['data']) // 1024} KB)" for f in files))
                for f in files:
                    if save:
                        Path(save, f["name"]).write_bytes(f["data"])
        for p in dict.fromkeys(file_problems):
            log.error("ALERT: file not shared: %s", p)
        return 1 if file_problems else 0

    failed = False
    for ch, secs, files_for in targets:
        try:
            file_problems += post_roundup(ch, header, secs, files_for, workspace)
            log.info("Posted the roundup to %s with %d org(s) in its thread.", ch, len(secs))
        except (mel.SlackAuthError, mel.SlackCallError) as e:
            log.error("ALERT: failed to post the roundup to %s (%s).", ch, e)
            failed = True
    for p in dict.fromkeys(file_problems):
        log.error("ALERT: file not shared: %s", p)
    if any("missing_scope" in p for p in file_problems):
        log.error("ALERT: the Mel intake Slack app needs the files:write user scope to attach "
                  "files (README, 'Slack app').")
    return 1 if failed or file_problems else 0


if __name__ == "__main__":
    sys.exit(main())
