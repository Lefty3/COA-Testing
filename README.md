# Discord Test-Results Bot

Watches a Discord **category** (e.g. "Test Results" in your screenshot) and, whenever a PDF is uploaded to any channel inside it — now or in the future — extracts the test data with Claude and appends a row to a Google Sheet. A second tab shows a live KPI dashboard.

## What it captures

The bot doesn't store Discord metadata as the main payload — it stores **whatever the PDF says about the test**. Specifically, each row contains:

| Column | Meaning |
|---|---|
| source_channel | Channel the PDF was uploaded to (e.g. `nova-tirz-10mg-black`) |
| source_link | Direct URL to the PDF on Discord's CDN |
| file_name | Original filename |
| captured_at | Timestamp the bot processed it |
| compound | Active ingredient / molecule (parsed from PDF, falls back to channel name) |
| dose_mg | Numeric dose |
| vial_color | Vial colour (from PDF or channel name) |
| batch_lot | Manufacturer lot / batch ID |
| test_date | Date the test was performed |
| lab | Testing lab / vendor |
| method | HPLC, LC-MS, NMR, etc. |
| purity_pct | Numeric purity percent |
| mass_spec_match | yes / no / unknown |
| endotoxin | Endotoxin result |
| sterility | Sterility test result |
| appearance | Physical appearance description |
| result | pass / fail / inconclusive |
| notes | Anything else worth knowing |

## Dashboard tab

The `Dashboard` tab is **formula-driven** — it recalculates automatically each time the master tab gains a new row. It shows:

- Total tests, unique compounds, average purity %, pass rate, failed/inconclusive counts
- Tests captured in the last 7 / 30 days
- Most recent test date
- Tests **by compound** (pivot, ordered by count)
- Tests **by lab** (pivot)
- Average purity **by compound**

You can add your own charts on top of these pivots — they're regular Google Sheets ranges.

## How it triggers

- **Live**: an `on_message` listener fires the second a PDF is uploaded to any channel under the watched category. New channels added later are picked up automatically (every channel's `category_id` is checked at message time).
- **New-channel notice**: `on_guild_channel_create` logs when a new channel appears in the category.
- **Weekly safety-net sweep**: every Monday at 13:00 UTC (configurable) the bot re-walks every channel in the category and ingests anything it missed.
- **On demand**: `/sweep_tests` to run the sweep right now.

PDFs are deduped in two ways: a local `state.json` of processed attachment IDs, and a cross-check against the `source_link` column in the sheet (so a fresh install with a wiped state file won't re-ingest existing rows).

## Project layout

```
discord-test-results-bot/
├── bot.py              # main entry, listeners, sweep loop, slash commands
├── pdf_extractor.py    # downloads PDFs and asks Claude for structured fields
├── sheets_client.py    # gspread wrapper + dashboard formula installer
├── config.py           # env loader
├── requirements.txt
├── .env.example
└── .gitignore
```

## 1. Create the Discord application

1. https://discord.com/developers/applications → **New Application** → **Bot** → copy token = `DISCORD_TOKEN`.
2. Enable **Message Content Intent** under Privileged Gateway Intents.
3. OAuth2 → URL Generator → scopes `bot` + `applications.commands`, permissions: View Channels, Read Message History, Send Messages, Use Slash Commands. Invite the bot to your server.
4. Right-click the **"Test Results" category header** (not a channel) → **Copy ID**. That's `TEST_RESULTS_CATEGORY_ID`.

## 2. Get an Anthropic API key

https://console.anthropic.com/ → API Keys → create → `ANTHROPIC_API_KEY`.

## 3. Create a Google Cloud service account

1. Go to https://console.cloud.google.com/ and create (or pick) a project.
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **APIs & Services → Credentials → Create credentials → Service account**. Give it any name (e.g. `discord-test-bot`), no roles needed.
4. Open the created service account → **Keys** tab → **Add key → Create new key → JSON**. The JSON file downloads — save it as `service-account.json` next to `bot.py`.
5. Copy the service account's email (looks like `discord-test-bot@<project>.iam.gserviceaccount.com`).
6. Create a blank Google Sheet, **Share** it with that email as **Editor**, and copy the spreadsheet ID from the URL (`docs.google.com/spreadsheets/d/<THIS_PART>/edit`).

## 4. Configure and run

```bash
cd discord-test-results-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in DISCORD_TOKEN, TEST_RESULTS_CATEGORY_ID, ANTHROPIC_API_KEY,
# GOOGLE_SERVICE_ACCOUNT_FILE, and SPREADSHEET_ID

python bot.py
```

On first launch the bot creates the `All Tests` and `Dashboard` tabs and installs all dashboard formulas. Run `/sweep_tests` once to backfill every PDF already in the category. After that, uploads land in the sheet within seconds of being posted.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token |
| `TEST_RESULTS_CATEGORY_ID` | — | Category to watch |
| `IGNORE_CHANNEL_PATTERNS` | _(empty)_ | Comma-separated substrings; channels matching are skipped (e.g. `guidelines,freely-shared`) |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `CLAUDE_MODEL` | `claude-sonnet-4-5` | Model for PDF extraction |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | _(empty)_ | Path to JSON key file (local dev) — use one of this OR the next var |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | _(empty)_ | Raw JSON contents of the key (Railway / Fly / Docker) |
| `SPREADSHEET_ID` | — | Google Sheets ID |
| `MASTER_TAB` | `All Tests` | Master tab name |
| `DASHBOARD_TAB` | `Dashboard` | KPI tab name |
| `SWEEP_DAY` | 0 (Mon) | Weekly sweep day-of-week (UTC) |
| `SWEEP_HOUR` | 13 | Weekly sweep hour (UTC) |
| `STATE_PATH` | `state.json` | Local dedupe state file |

## Deploying on Railway

1. **Push the repo to GitHub.** Make sure `.env` and `service-account.json` are NOT committed — they're already in `.gitignore`.
2. **Create a Railway project** → New → Deploy from GitHub repo → pick this repo. Railway autodetects the Python project and runs `python bot.py`.
3. **Add environment variables** under the service's *Variables* tab. Paste each of these — Railway lets you bulk-paste KEY=VALUE pairs:
   - `DISCORD_TOKEN`
   - `TEST_RESULTS_CATEGORY_ID`
   - `ANTHROPIC_API_KEY`
   - `SPREADSHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — open `service-account.json`, copy the **entire file contents** (the `{ ... }` JSON object), and paste it as the value. Do NOT base64-encode it; Railway accepts multi-line JSON values as-is.
   - Optionally: `IGNORE_CHANNEL_PATTERNS`, `CLAUDE_MODEL`, `MASTER_TAB`, `DASHBOARD_TAB`, `SWEEP_DAY`, `SWEEP_HOUR`.
4. **Redeploy** (Railway auto-redeploys on variable change). Check the *Deploy Logs* tab — you should see `Logged in as YourBotName`. The crash `RuntimeError: Missing required env vars` means a required variable above is unset or misspelled.
5. **Persistent state (optional).** The bot writes `state.json` to dedupe processed attachments. Railway's filesystem is ephemeral, so each redeploy resets it. That's fine because the bot also cross-checks against the sheet's `source_link` column — duplicates won't be created. If you want true persistence, attach a Railway **Volume** mounted at `/data` and set `STATE_PATH=/data/state.json`.

### Troubleshooting

- `RuntimeError: Missing required env vars: ...` — exactly one or more of the listed vars is unset in Railway. The error message names them.
- `gspread.exceptions.APIError ... permission` — the spreadsheet hasn't been shared with the service-account email as Editor.
- `discord.errors.PrivilegedIntentsRequired` — turn on *Message Content Intent* in the Discord developer portal.
- Bot logs in but never captures anything — verify `TEST_RESULTS_CATEGORY_ID` is the **category** ID (right-click the category header), not a channel ID.

## Slash commands

- `/sweep_tests` — run a full category sweep right now (admins).
- `/test_status` — show last sweep time and schedule.
- `/test_query <compound>` — fuzzy-match the compound (or channel substring) and reply with an ephemeral summary embed: total tests, average purity, date range, pass/fail/inconclusive counts, the latest test's details with a PDF link, and links to the 5 most recent tests. Examples: `/test_query tirz`, `/test_query bpc157`, `/test_query nova-ss31-50mg`.

## Cost notes

Claude extracts fields from one PDF per row. Typical COA PDFs are 1-3 pages and the call is ~$0.005–0.02 depending on the model. The Dashboard tab is pure spreadsheet formulas — no extra API cost as data grows.

## Extending the schema

To add columns: append the new field name to `TEST_RESULT_FIELDS` in `pdf_extractor.py` and to `MASTER_HEADERS` in `sheets_client.py`, then add it to the JSON schema in `_SYSTEM` so Claude knows to extract it. The bot will auto-rewrite the header row on next launch.
