# MEL intake

Turns Slack charity-performance threads into rows in the Airtable **MEL Notes** table.

You paste Slack thread links into **#mel-intake** (channel `C0B9WG6UY4W`, workspace
`ceincubationprogram.slack.com`). Once a day a launchd job reads that channel, fetches
each full thread via your local Slack skill (running as your account through browser
tokens, so it sees everyone's messages), summarises each thread for MEL with **Claude
Sonnet 4.6**, and upserts one row per thread into MEL Notes — deduped on a `Thread Key`.

Authorisation is just "who can post to #mel-intake": any thread link that appears there
gets processed. No allowlist, no emoji trigger.

---

## Files

| File | What it is |
|---|---|
| `mel_intake.py` | The whole job. |
| `.env.example` | Template for your secrets + config. Copy to `~/.mel-intake/.env`. |
| `com.aidanalexander.melintake.plist` | launchd job (fires 10:00, retries to 17:30; runs once/day). |
| `README.md` | This file. |

**Code lives here (in Google Drive); secrets and state live in `~/.mel-intake/` (outside
Drive)** so your API keys are never synced to the cloud. `~/.mel-intake/` holds:
`.env` (secrets), `state.json` (watermark + confirmation tracking), and the log files.

---

## What gets written to MEL Notes

One row per thread. Merge key is **`Thread Key`** = `<channel_id>:<parent_thread_ts>`.

| Field | Source |
|---|---|
| `Thread Key` | `channel_id:parent_thread_ts` (plain text — the dedup key) |
| `Thread URL` | Canonical permalink to the thread |
| `Channel` | Channel name (e.g. `#charity-updates`), or the ID if unresolved |
| `Summary` | Claude Sonnet 4.6 MEL summary (plain long-text field added for this job) |
| `Focus` | Short (<10 word) focus line from the summary, e.g. "Pilot results; driving teacher uptake" |
| `Participants` | Distinct display names, in order of first posting |
| `Date` | Date of the thread's parent message |
| `Raw Messages` | Cleaned, name-resolved transcript (also feeds your existing `AI Summary` field) |
| `Captured By` | Display name of whoever pasted/forwarded the link in #mel-intake (falls back to `MEL_CAPTURED_BY` if unresolved) |
| `Charity` | Linked to the Charities table **only on a confident name/acronym match**, else left blank |

> The table already had an `AI Summary` (Airtable AI / `aiText`) field, which the API
> cannot write to. This job writes its own plain `Summary` field instead, and still fills
> `Raw Messages` so your `AI Summary` field keeps generating as a cross-check.

Three fields (`Summary`, `Participants`, `Date`) were added to the table during setup.

---

## Prerequisites

1. **Slack skill configured** for the `ceincubationprogram` workspace, with working
   browser tokens. Verify:
   ```bash
   python3 ~/.claude/skills/slack/scripts/slack_client.py -w ceincubationprogram auth
   ```
   should print `"ok": true` and `https://ceincubationprogram.slack.com/`.

2. **Python deps** (already present on this Mac): `requests` and `anthropic`.
   If you ever need to reinstall:
   ```bash
   /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pip install --upgrade requests anthropic
   ```

3. **Airtable personal access token** with `data.records:read` **and**
   `data.records:write`, and this base added to the token's access (Builder/Editor on the
   base). Create at <https://airtable.com/create/tokens>. (`read` is needed for the
   Charity auto-link; the upsert itself is `write`.)

4. **Anthropic API key** for the summaries.

---

## Setup

```bash
mkdir -p ~/.mel-intake
cp "/Users/aidanalexander/Library/CloudStorage/GoogleDrive-aidan@charityentrepreneurship.com/My Drive/Claude Code/mel-intake/.env.example" ~/.mel-intake/.env
# then edit ~/.mel-intake/.env and paste in AIRTABLE_API_KEY and ANTHROPIC_API_KEY
```

The base/table IDs and Slack defaults are already filled in; you normally only edit the
two secret lines.

---

## Run it once manually to test

First, a **dry run** — does everything (reads the channel, fetches threads, summarises)
but writes nothing to Airtable and posts no Slack replies. It prints each row it *would*
write:

```bash
cd "/Users/aidanalexander/Library/CloudStorage/GoogleDrive-aidan@charityentrepreneurship.com/My Drive/Claude Code/mel-intake"
python3 mel_intake.py --dry-run
```

Tips for the first test:
- `--since-days 30` forces a 30-day lookback regardless of saved state.
- `--limit 50` caps how many recent #mel-intake messages are scanned.

When the dry run looks right, do a **real run**:

```bash
python3 mel_intake.py
```

This writes the rows and, on this default (script-summarises) path, posts a short
threaded reply — **"MEL bot says: Captured to the Airtable"** — under each intake message it captured.
The reply is posted only after the Airtable upsert succeeds, and only once per intake
message (tracked in `state.json`), so re-runs never double-post. If a confirmation reply
fails, it's logged but not treated as a job failure — the row is already saved.

Re-running is always safe: the Airtable upsert dedups on `Thread Key`, so overlapping
reads just update the same rows.

---

## Schedule it with launchd (daily, with afternoon retries)

```bash
# 1. Copy the plist into LaunchAgents
cp "/Users/aidanalexander/Library/CloudStorage/GoogleDrive-aidan@charityentrepreneurship.com/My Drive/Claude Code/mel-intake/com.aidanalexander.melintake.plist" ~/Library/LaunchAgents/

# 2. Load it
launchctl load ~/Library/LaunchAgents/com.aidanalexander.melintake.plist

# 3. (optional) Run it right now to confirm the scheduled invocation works
launchctl start com.aidanalexander.melintake
```

It tries at **10:00**, then retries at **12:00, 14:00, 16:00, 17:30** — but **runs at
most once per day, and never on Saturdays** (Saturday links are picked up Sunday). The first attempt that completes records the date in `state.json`,
and the later attempts that day just skip. The extra times are a safety net: if the Mac
was asleep/closed at 10:00, it runs whenever it next wakes (and a Mac asleep across
several fire times wakes and runs once, not once per missed slot). Unprocessed links are
never lost during downtime — they sit in #mel-intake and get picked up on the next run
via the watermark in `state.json`.

A same-day **manual** run is blocked by the once-a-day guard; use `--force` to override:
```bash
python3 mel_intake.py --force
```
(`--dry-run` always runs and never counts as the day's run.)

**Change the times:** edit the `StartCalendarInterval` `<array>` in
`~/Library/LaunchAgents/com.aidanalexander.melintake.plist` — add/remove/adjust the
`<dict>` entries (each is one `Hour`/`Minute` fire time).

After editing, reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.aidanalexander.melintake.plist
launchctl load   ~/Library/LaunchAgents/com.aidanalexander.melintake.plist
```

**Stop it:**
```bash
launchctl unload ~/Library/LaunchAgents/com.aidanalexander.melintake.plist
```

---

## Run on GitHub Actions instead (always-on, no Mac needed)

launchd only runs while this Mac is awake. To run in the cloud on a schedule, the repo
includes `.github/workflows/mel-intake.yml`. Setup:

1. **Put this folder in a *private* GitHub repo** (the repo root is this `mel-intake/`
   folder). It must be private — `state/state.json` and the workflow live in it, and the
   secrets act on AIM's Slack/Airtable.
2. **Add three repository secrets** (Settings → Secrets and variables → Actions → New
   repository secret), names matching `.env.example`:
   - `SLACK_USER_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `AIRTABLE_API_KEY`
3. Push. It runs on the schedule below, and can be run on demand from the **Actions** tab
   → *MEL intake* → **Run workflow**.

How it works on Actions:
- **No `.env`** — secrets are injected as environment variables from the repo secrets.
- **State persists** by committing `state/state.json` back to the repo after each run
  (`MEL_INTAKE_HOME` points at `state/`); the runner is otherwise ephemeral.
- **Timezone:** the workflow sets `TZ=Europe/London`, so the once-a-day guard and Saturday
  skip use London time. Cron is UTC — the fires are `09:00` and `13:00` UTC (~10:00 and
  ~14:00 London in summer); the second is a safety net the once-a-day guard no-ops. Edit
  the `cron:` lines to change times.
- **Failure alerts:** a failed run shows red in the Actions tab and GitHub emails you —
  the out-of-band signal that doesn't depend on Slack being up.

Caveats: GitHub's scheduled runs are best-effort and can be delayed or rarely skipped under
load (fine for a daily job — the watermark catches anything missed next run), and cron is
UTC so a BST/GMT switch shifts the London time by an hour.

> ⚠️ **If you move to GitHub Actions, turn off the launchd job** so they don't both process
> the channel (which would double-post the "Captured" replies — Airtable rows still
> dedupe): `launchctl unload ~/Library/LaunchAgents/com.aidanalexander.melintake.plist`

---

## Newsletter intake (a second, separate job)

`newsletter_intake.py` is an independent job: it imports **charity newsletters** that
Morgan forwards into #mel-intake (via Slack's email-to-channel integration) into a dedicated
Airtable table, **"Newsletter Intake"** (`tblr8m5vdnya1PmzD`) — separate from the
thread→MEL-Notes bot above.

- **Source:** Slackbot email posts in #mel-intake (a message whose `files[]` has a
  `filetype:"email"` entry carrying `subject`, `from`, and the full `plain_text` body
  inline). Google "forwarding confirmation" emails are skipped.
- **Per newsletter (one row):** `Subject`, `Date`, `From`, a concise **`What's covered`**
  (a few short bullets, Claude Sonnet 4.6), the full **`Raw email`** copy, an `Email link`
  (the "view in browser" URL), a `Slack link` to the source message, and `Organization`
  linked to the matching Charities record when confident.
- **Dedup:** upserts on `Source Msg TS` (the Slack message ts) — no state file; it scans a
  lookback window (`MEL_NEWSLETTER_LOOKBACK_DAYS`, default 14) each run.
- **Run it:** `python3 newsletter_intake.py` (`--dry-run` to preview, `--since-days N`).
- **Schedule:** its own workflow `.github/workflows/newsletter-intake.yml`, daily, using
  the same three repo secrets.
- **Weekly digest:** `newsletter_digest.py` (workflow `.github/workflows/newsletter-digest.yml`,
  Mondays) reads the past week's rows from this table and posts a digest to **#aim-staff**
  (`MEL_DIGEST_CHANNEL`, default `CMS6V5XEJ`), **grouped by organisation** — each org headed
  by a link to its "view in browser" email, with a few bullets of noteworthy updates
  (hiring, progress, new evidence/evaluations, funding, etc.). Preview with
  `python3 newsletter_digest.py --dry-run`.

> This writes to its **own** new table, so it does not conflict with any older newsletter
> automation pointing at the pre-existing `Newsletters` table — they populate different
> tables. Retire the old one whenever you like.

## Logs & state

- `~/.mel-intake/mel_intake.log` — the job's own log (what it read, wrote, skipped).
- `~/.mel-intake/launchd.out.log` / `launchd.err.log` — launchd's stdout/stderr capture.
- `~/.mel-intake/state.json` — `last_processed_ts` (watermark), the set of intake
  messages already confirmed, and your cached Slack user ID for failure DMs.

The watermark only advances past intake messages that fully succeeded; a thread that
errored transiently is retried on the next run.

---

## Reliability: Slack auth

The job authenticates with a **long-lived user OAuth token** from the "Mel intake" Slack
app (`xoxp-…`), stored in the slack skill's `~/.claude/skills/slack/config.json` under the
`ceincubationprogram` workspace's `xoxc_token` field. Unlike the old browser tokens
(`xoxc`/`xoxd`), this does **not** expire, so routine re-authentication is no longer needed.

If Slack auth ever fails anyway (token revoked, app uninstalled, a scope removed), the job
**fails loudly**: an `ALERT` line in the log, a macOS notification, and a best-effort Slack
DM. To fix: open the "Mel intake" app at <https://api.slack.com/apps> → **OAuth &
Permissions** → reinstall / copy a fresh **User OAuth Token**, and set it as `xoxc_token`
in `~/.claude/skills/slack/config.json`. Nothing is lost — links wait in #mel-intake and
get picked up on the next successful run.

The app has only the scopes the job needs: `channels:history`, `groups:history`,
`channels:read`, `groups:read`, `users:read`, `chat:write`, `files:read`. The slack skill's
own search/digest/export features additionally need `search:read` — add it to the app and
reinstall if you want those on this workspace.

---

## Notes

- **Charity auto-link** only fills the `Charity` link on a confident exact name/acronym
  match against the Charities table; otherwise it's left blank (a wrong link is worse than
  none). Set `MEL_LINK_CHARITIES=false` in `.env` to turn it off entirely.
- **Links inside threads** are kept as readable text (`label (url)`, or the bare URL) in
  the transcript/summary, and link unfurls (title/snippet previews) are included — but
  links are **not** followed, so linked-document contents aren't pulled in.
- **Attached PDFs are read**: text is extracted (`pypdf`), capped (~12k chars via
  `MEL_PDF_TEXT_MAXCHARS`) and fed into the summary, so the row reflects the report, not
  just the caption. Other file types (images, slides, `.docx`) are **named** in the
  transcript (`[attached file(s): ...]`) but not read. Charts/figures embedded as images
  in a PDF aren't captured by text extraction. Toggle with `MEL_READ_PDFS=false`.
  - *Tier note:* the script sends extracted **text**, not the raw PDF, because the
    Anthropic org's rate limit (10,000 input tokens/min) can't fit a full multi-page
    report. Raising the API tier would let you send the actual PDF (figures included) and
    remove the per-minute throttling; until then, a backlog of threads processes a bit
    slowly as the job waits out the limit between calls.
- All Slack access goes through the local skill CLI (`auth`, `history`, `replies`,
  `user-lookup`, `channels`, `send`) on the `ceincubationprogram` workspace.
- The Slack skill ships without its `data/` directory (gitignored), which breaks
  `user-lookup`; the script recreates it on every run so name attribution can't silently
  break after a skill reinstall.
