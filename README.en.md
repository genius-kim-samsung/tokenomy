# Tokenomy

A local ledger for your AI coding token spend. Tokenomy parses your
**local** Claude Code / Codex CLI session logs and automatically reads the
official usage API — showing official limit vs. remaining, spend forecasts,
cost per project/session, and cache-efficiency signals.

> Korean README: [README.md](README.md)

## Who it's for

Anyone using Claude Code and/or Codex CLI who wants to track their usage and limits.

- **Enterprise / pay-as-you-go**: the official API provides a USD limit, so you get a
  live remaining-spend and depletion forecast immediately.
- **Personal subscription**: flat-rate accounts have no USD budget, but the official API
  returns a rate-window (5 h / 7 d utilisation %) — the key actionable signal.

If official data is unavailable (no credentials, or `TOKENOMY_SKIP_OFFICIAL_FETCH` set),
the app falls back to a **usage-only view** driven by local JSONL logs.

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
   **Close the window to quit.**
4. When a new version ships, the dashboard shows an update banner — click it,
   download the new `Tokenomy.exe`, and overwrite the old one.

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
  "pricing_overrides": {}
}
```

- `tracked_providers`: which AI tools to fetch official usage for and show on the
  dashboard. Auto-seeded on first run from whichever credential files are present
  (`~/.claude/.credentials.json`, `~/.codex/auth.json`).
  Limits and remaining are sourced from the official API — enterprise/pay-as-you-go
  accounts see a USD limit; personal subscription accounts see a rate-window (%).
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

> The History and Analysis pages support a **week/month toggle** and a **custom date
> range** for querying.

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

- **Default-on** for providers in `tracked_providers`; accounts without a USD
  limit (personal subscription) fall back to a usage-only view.
- Set `TOKENOMY_SKIP_OFFICIAL_FETCH` to disable all network calls (offline /
  CI / testing).
- `min_interval_minutes` (default 5) throttles how often we call the API —
  controls *our* call frequency, not the provider's quota.

## Adding a parser for another tool

Tokenomy normalizes each tool's logs into `UsageRecord` (see
`tokenomy/parser.py`). To support another CLI, write a module that discovers
its log files and yields `UsageRecord`s, then ingest them via
`tokenomy.db.ingest_records(conn, records, pricing)` — see
`tokenomy/codex_parser.py` as a reference implementation.

## License

MIT — see [LICENSE](LICENSE).
