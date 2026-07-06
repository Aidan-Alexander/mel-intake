# MEL intake

A small set of automations that feed **Ambitious Impact (AIM)'s** monitoring, evaluation &
learning (MEL) work from Slack and forwarded newsletters into Airtable, and push a weekly
summary back to staff on Slack.

Everything runs on **GitHub Actions** on a schedule (no server, no always-on Mac needed).
Each job is a small self-contained Python script that talks directly to the **Slack Web
API**, the **Anthropic API** (Claude Sonnet 4.6, for summarisation), and the **Airtable Web
API**.

---

## The four jobs

| Job | Script | What it does | Schedule | Output |
|---|---|---|---|---|
| **Thread intake** | `mel_intake.py` | Captures charity-performance **Slack threads** people flag | Daily | 1 row per thread → **MEL Notes** table |
| **Newsletter intake** | `newsletter_intake.py` | Imports charity **newsletters** forwarded into #mel-intake | Daily | 1 row per newsletter → **Newsletter Intake** table |
| **Weekly digest** | `newsletter_digest.py` | Summarises the week's newsletters, grouped by org | Mondays | Message → **#aim-staff** |
| **Coverage check** | `charity_newsletter_check.py` | Flags active charities we've had **no** newsletter from | Quarterly | DM → **Morgan Fairless** |

All four share one input channel (**#mel-intake**) and one Airtable base, and use the same
three secrets.

---

## How it works

```
                         #mel-intake  (Slack)
                          |                  \
        thread permalinks |                   \ forwarded-email posts (Slackbot)
                          v                    v
                   mel_intake.py         newsletter_intake.py
                   (fetch thread,        (read email body,
                    summarise)            summarise)
                          |                    |
                          v                    v
     Airtable: MEL Notes  <----   base   ---->  Airtable: Newsletter Intake
                                                    |                 |
                        newsletter_digest.py  <-----+                 |
                        (weekly, by org) --> #aim-staff               |
                        charity_newsletter_check.py  <----------------+
                        (quarterly) --> DM Morgan (gaps vs Charities table)
```

- **Slack** is accessed directly via the Web API using a **long-lived user OAuth token**
  (`SLACK_USER_TOKEN`, an `xoxp-…` token from the "Mel intake" Slack app). It acts as Aidan,
  so it can read any thread/channel Aidan can see and post as him.
- **Summarisation** is Claude **Sonnet 4.6** via the Anthropic API.
- **Airtable** reads/writes go through the Web API with a personal access token.
- `mel_intake.py` doubles as the **shared library** — the other three scripts
  `import mel_intake` for its Slack, Airtable, and helper functions.

Authorisation is simply "who can post to #mel-intake": any thread link or forwarded
newsletter that lands there is processed. No allowlist, no emoji trigger.

---

## Repo layout

```
mel_intake.py                     # thread intake + shared Slack/Airtable/Anthropic helpers
newsletter_intake.py              # newsletter -> Newsletter Intake table
newsletter_digest.py              # weekly digest -> #aim-staff
charity_newsletter_check.py       # quarterly coverage check -> DM Morgan
requirements.txt                  # requests, anthropic, pypdf
.env.example                      # secret/config template (for local runs)
com.aidanalexander.melintake.plist# optional macOS launchd job for the thread bot (legacy)
state/state.json                  # thread-intake watermark (committed back by CI)
.github/workflows/
  mel-intake.yml                  # thread intake (daily)
  newsletter-intake.yml           # newsletter intake (daily)
  newsletter-digest.yml           # weekly digest (Mondays)
  charity-newsletter-check.yml    # coverage check (quarterly)
```

---

## Airtable (base `app6tmBJhcfCS7FLs`)

**MEL Notes** (`tblbtQqlp4z9uky47`) — one row per captured thread, deduped on **`Thread Key`**
(`<channel_id>:<parent_thread_ts>`):

| Field | Contents |
|---|---|
| `Thread Key` | dedup / merge key |
| `Thread URL`, `Channel`, `Date`, `Participants` | thread metadata |
| `Summary` | Claude MEL summary |
| `Focus` | short (<10-word) focus line |
| `Raw Messages` | cleaned, name-resolved transcript |
| `Captured By` | who pasted/forwarded the link |
| `Charity` | linked to the Charities table on a confident name match, else blank |

**Newsletter Intake** (`tblr8m5vdnya1PmzD`) — one row per newsletter, deduped on
**`Source Msg TS`** (the Slack message ts):

| Field | Contents |
|---|---|
| `Subject`, `Date`, `From` | email metadata |
| `What's covered` | a few concise bullets (Claude) |
| `Raw email` | full plain-text copy |
| `Email link` | the "view in browser" URL |
| `Slack link` | permalink to the source Slack message |
| `Organization` | linked to the Charities table on a confident match |

The **Charities** table (`tblSsWP0lp1fH9kk6`) is read-only here — used to link records and,
in the coverage check, to enumerate active charities (`Status = Active`).

---

## Secrets & config

Three secrets (names identical whether in a local `.env` or as GitHub Actions repo secrets):

| Secret | What |
|---|---|
| `SLACK_USER_TOKEN` | `xoxp-…` user token from the "Mel intake" Slack app |
| `ANTHROPIC_API_KEY` | for Claude Sonnet 4.6 summaries |
| `AIRTABLE_API_KEY` | Airtable PAT with `data.records:read` + `write` on the base |

Everything else (base/table IDs, channel IDs, model, schedules, lookback windows) has
sensible defaults baked into the scripts and can be overridden by env vars — see
`.env.example` for the full list (`SLACK_WORKSPACE`, `MEL_INTAKE_CHANNEL`,
`AIRTABLE_*_TABLE_ID`, `MEL_DIGEST_CHANNEL`, `MEL_MORGAN_USER_ID`,
`MEL_NEWSLETTER_LOOKBACK_DAYS`, `MEL_COVERAGE_LOOKBACK_DAYS`, `MEL_READ_PDFS`, etc.).

The **Slack app** ("Mel intake", api.slack.com/apps) uses **user-token scopes**:
`channels:history`, `groups:history`, `channels:read`, `groups:read`, `users:read`,
`chat:write`, `files:read`. The token is long-lived (rotation off).

---

## Deployment — GitHub Actions (primary)

Repo: **github.com/Aidan-Alexander/mel-intake** (private).

1. The three secrets are set under **Settings → Secrets and variables → Actions**.
2. Each job has its own workflow (see the repo layout). They run on cron and can also be
   run on demand: **Actions tab → pick the workflow → Run workflow**.
3. Schedules (cron is **UTC**; `TZ=Europe/London` is set so any date logic uses London time):
   - **Thread intake** — `mel-intake.yml`, 09:00 + 13:00 (once-a-day guard + Saturday skip
     in code; second fire is a safety net). Commits `state/state.json` back to the repo so
     the watermark persists across runs.
   - **Newsletter intake** — `newsletter-intake.yml`, 08:00 daily. Stateless (dedupes via
     Airtable upsert + a 14-day lookback).
   - **Weekly digest** — `newsletter-digest.yml`, Mondays 08:00.
   - **Coverage check** — `charity-newsletter-check.yml`, 09:00 on the 1st of
     Mar/Jun/Sep/Dec.

**Failure alerts:** a failed run turns red in the Actions tab and GitHub emails you — an
out-of-band signal that doesn't depend on Slack being up. `mel_intake.py` also attempts a
Slack DM on auth failure.

Caveat: GitHub's scheduled runs are best-effort and can be delayed/rarely skipped under
load — fine here, since the watermark / dedup catch anything missed on the next run.

### Optional: run the thread bot locally via launchd (legacy)

`mel_intake.py` can also run on a Mac via `com.aidanalexander.melintake.plist` (reads
secrets from `~/.mel-intake/.env`, keeps state in `~/.mel-intake/`). This predates the
Actions setup and is **currently unloaded** — don't run both at once or you'll double-post
the "Captured" confirmations (Airtable rows still dedupe). To use it instead:
`cp` the plist into `~/Library/LaunchAgents/` and `launchctl load` it.

---

## Running / testing locally

Install deps once: `pip install -r requirements.txt`. Provide the three secrets via a local
`~/.mel-intake/.env` (copy `.env.example`). Every script supports `--dry-run` (does the work
but writes nothing / posts nothing) and prints what it *would* do:

```bash
python3 mel_intake.py --dry-run                 # thread intake  (--since-days N, --limit N, --force)
python3 newsletter_intake.py --dry-run          # newsletter intake  (--since-days N)
python3 newsletter_digest.py --dry-run          # weekly digest  (--since-days N)
python3 charity_newsletter_check.py --dry-run   # coverage check  (--since-days N)
```

Real runs are idempotent: thread/newsletter intake upsert on their dedup keys, so
re-running only updates existing rows. `mel_intake.py` posts a threaded
**"🤖 … Captured to the Airtable"**-style reply once per intake message (tracked in state).

---

## Behaviour notes

- **Charity linking** only fills the `Charity`/`Organization` link on a confident exact
  name/acronym match — a wrong link is worse than none. (`MEL_LINK_CHARITIES=false` to disable
  on the thread bot.)
- **Attached PDFs are read** on the thread bot: text is extracted with `pypdf` (per-page, so
  a bad page doesn't sink the whole file), capped (~12k chars), and fed into the summary.
  Other file types are named but not read; charts/figures embedded as images aren't captured.
- **Links inside threads** are kept as readable text and their unfurl previews are included,
  but links are not followed (the linked page isn't fetched).
- **Newsletter emails** arrive as Slack email-integration posts — the `subject`, `from`, and
  `plain_text` body are inline in the message's `files[]`. Google "forwarding confirmation"
  emails are skipped.
- **HTML-only newsletters** (MailerLite, some Mailchimp setups) send only a "can't display
  HTML — view in browser" stub in the plain-text part. When intake sees such a stub it
  downloads the full HTML (Slack keeps it, fetched with the same user token) and converts it
  to text, so the real content still reaches the summary and digest. If recovery ever fails,
  the digest falls back to listing the org with a link to open the email.
- **The digest** groups by organisation, links each org to its "view in browser" email, and
  is scoped to each org's **own** news — a recommender/regranter's grantee wins are excluded.
  Messages are prefixed with 🤖 so staff know they're automated.
- **Anthropic rate limit:** the org is on a tight 10,000 input-tokens/min tier, so the
  scripts cap input sizes and the SDK backs off/retries. A backlog processes a bit slowly;
  raising the API tier removes the throttle.

---

## Reliability: Slack auth

The `SLACK_USER_TOKEN` is a long-lived user OAuth token and does **not** expire, so routine
re-authentication isn't needed (this replaced the old browser tokens, which expired ~weekly).
If it ever fails (token revoked, app uninstalled, a scope removed), the run fails loudly
(red in Actions + email; thread bot also DMs). To fix: at api.slack.com/apps → **Mel intake**
→ **OAuth & Permissions**, reinstall / copy a fresh **User OAuth Token**, and update the
`SLACK_USER_TOKEN` secret (and your local `~/.mel-intake/.env`). Nothing is lost — inputs
wait in #mel-intake and are picked up on the next successful run.
