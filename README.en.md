# Tokenomy

A local ledger for your AI coding token spend. Tokenomy parses your
**local** Claude Code / Codex CLI session logs and automatically reads the
official usage API — showing official limit vs. remaining, an end-of-month
surplus/shortfall forecast, cost per project/session, and cache-efficiency signals.

> Korean README: [README.md](README.md)

## Who it's for

Anyone using Claude Code and/or Codex CLI who wants to track their usage and limits.

- **Enterprise / pay-as-you-go**: the official API provides a USD limit, so you get a
  live remaining-spend and depletion forecast immediately.
- **Personal subscription**: flat-rate accounts have no USD budget, but the official API
  returns a rate-window (5 h / 7 d utilisation %) — the key actionable signal.

If you use more than one AI, you choose which ones are **active** (`tracked_providers`) —
the dashboard's "all" is the **sum of active AIs**, not the whole DB, and AIs that have a
USD limit are pooled together for a single end-of-month forecast.

If official data is unavailable (no credentials, limit-less account, or
`TOKENOMY_SKIP_OFFICIAL_FETCH` set), the app falls back to a **usage-only view**
driven by local JSONL logs.

## What it shows

- **Dashboard** — this month's combined forecast (active AIs pooled in USD for an
  end-of-month surplus/shortfall estimate), total spend (sum of active AIs), a trend
  chart (limit & projected lines), token composition, an efficiency coach, cost per
  project (Top 10), and recent expensive sessions for review (Top 10).
- **Official usage cards** — a per-provider gauge (5-hour / 7-day / monthly official
  buckets) plus a "today $ · this week $" glance. Colour encodes threshold, texture
  encodes official vs. estimated.
- **History (local)** — usage from local JSONL logs, with a **week/month toggle** and a
  **custom date range**.
- **History (official)** — the past trajectory of official usage snapshots (depleting
  limits), drillable per day (once official data has accumulated).
- **By dimension** — break spend down by model, branch, etc. (same week/month toggle and
  date range).
- **Mini view** — a small glance window that **swaps exclusively** with the main window
  (exe / native window only). Toggle it from the sidebar's "⊟ Mini view".

## Privacy

- Parses token **metadata** (tokens, time, project, model) plus a **short excerpt
  of the first user prompt** (for session identification). **Full conversation
  content is never stored.**
- Runs fully locally. The web dashboard binds to `127.0.0.1` only — do not
  expose it to a network.

## Quick start (non-developer — Windows)

1. Download `Tokenomy.exe` from
   [Releases](https://github.com/genius-kim-samsung/tokenomy/releases/latest).
2. Double-click it. (If Windows SmartScreen warns, click **More info → Run
   anyway** — it's the normal warning for an unsigned personal tool.)
3. The Tokenomy app window opens with the dashboard. Data is stored under
   `C:\Users\<you>\.tokenomy\` (in the `data\` and `config\` subfolders).
   **The window's X button hides to the tray** (it does not quit) — right-click the
   tray icon → "Quit" to exit fully. Use the sidebar's **⊟ Mini view** to switch to a
   small at-a-glance window.
4. When a new version ships, the dashboard shows an update banner — click it,
   download the new `Tokenomy.exe`, and overwrite the old one.

On first run, if you've never used Claude Code / Codex (no credentials), you'll see a
**getting-started card** instead of an empty dashboard.

## Quick start (developer — from source)

```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

On Windows, double-click `start_tokenomy.bat` (ingest → dashboard → opens browser).

## Configuration

Edit `config/tokenomy.config.json`, or use the **Settings** page in the
dashboard (`/settings`):

```json
{
  "user_label": "me",
  "tracked_providers": ["claude", "codex"],
  "credit_to_usd": 0.04,
  "official_fetch": { "min_interval_minutes": 10 },
  "pricing_overrides": {}
}
```

- `tracked_providers`: the **active AIs** — which AI tools to fetch official usage for
  and show on the dashboard. The dashboard's "all" is the sum of this set. Auto-seeded on
  first run from whichever credential files are present (`~/.claude/.credentials.json`,
  `~/.codex/auth.json`). Limits and remaining are sourced from the official API —
  enterprise/pay-as-you-go accounts see a USD limit; personal subscription accounts see a
  rate-window (%).
- `credit_to_usd`: the rate used to convert Codex credits to USD (default 0.04). A
  separate constant from the token-pricing path.
- `official_fetch.min_interval_minutes`: the official-usage **auto-refresh interval**
  (minutes, default 10). It's both the polling cadence while a page is open and the
  minimum gap between automatic calls (the manual refresh button ignores it).
- `pricing_overrides`: override per-model rates if your billing differs from
  public list prices, or **add a new model** without waiting for an app update
  (takes effect on the next ingest):

  ```json
  "pricing_overrides": {
    "opus":    { "input": 4.0, "output": 20.0 },
    "gpt-5.6": { "provider": "codex", "input": 5.0, "output": 30.0, "cache_read": 0.5 }
  }
  ```

  Keys are partial-match tokens against the model id. A new key is added as a
  fresh pricing entry; a more specific key takes precedence over a broader one
  (e.g. `gpt-5.6` beats `gpt-5`). Unrecognised or suspect models are surfaced
  in the **Pricing Coverage** card on the Settings page.

## Data sources

- Claude Code: `~/.claude/projects/**/*.jsonl` (per-message usage + cache).
- Codex CLI: `~/.codex/sessions/**/rollout-*.jsonl` (per-session cumulative).

## Pricing

`config/pricing.json` ships with public API list prices. Update them as
providers change prices, or override per-user via `pricing_overrides`. Change a
price and the next ingest recalculates existing costs automatically — no need to
re-ingest your raw logs.

## Official usage fetch

Tokenomy automatically fetches live official usage for each provider listed in
`tracked_providers`. It uses the locally stored OAuth token in **read-only** mode
(no token refresh) and makes a single HTTP GET per provider (≤ 3 s, no retry).
**No PII is stored** — the access token and account ID are used only for the
request header and then discarded; only usage numbers are written to the local DB.

- **Fetching is decoupled from ingest.** Ingest only re-scans local JSONL; the dashboard
  drives official refresh — opening a page auto-polls (every `min_interval_minutes`), and
  a card's **refresh button** forces an immediate update, ignoring the interval.
- **Default-on** for providers in `tracked_providers`; accounts with no official data
  (e.g. limit-less) fall back to a usage-only view.
- Set `TOKENOMY_SKIP_OFFICIAL_FETCH` to disable all network calls (offline /
  CI / testing).

## Adding a parser for another tool

Tokenomy normalizes each tool's logs into `UsageRecord` (see
`tokenomy/parser.py`). To support another CLI, write a module that discovers
its log files and yields `UsageRecord`s, then ingest them via
`tokenomy.db.ingest_records(conn, records, pricing)` — see
`tokenomy/codex_parser.py` as a reference implementation. For official usage, see
`tokenomy/official_parser.py` (`OfficialBucket` + `credit_to_usd` conversion).

## License

MIT — see [LICENSE](LICENSE).
