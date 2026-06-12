# Tokenomy

A local "budget book" for your AI coding token spend. Tokenomy parses your
**local** Claude Code / Codex CLI session logs, then shows monthly burndown
against a budget you set, cost per project/session, and cache-efficiency
signals — so pay-as-you-go users don't blow past their budget mid-month.

> Korean README: [README.ko.md](README.ko.md)

## Who it's for

Pay-as-you-go (API-metered) users of Claude Code and/or Codex CLI who want to
track and cap their own monthly spend. Subscription (Pro/Max/Plus) users can
still track usage — costs show as *public-list-price estimates*.

## Privacy

- Parses only token **metadata** (tokens, time, project, model). **No prompt
  or conversation content is stored.**
- Runs fully locally. The web dashboard binds to `127.0.0.1` only — do not
  expose it to a network.

## Quick start (non-developer — Windows)

1. Download `Tokenomy.exe` from
   [Releases](https://github.com/genius-kim-samsung/tokenomy/releases/latest).
2. Double-click it. (If Windows SmartScreen warns, click **More info → Run
   anyway** — it's the normal warning for an unsigned personal tool.)
3. A console window opens and the dashboard opens in your browser. Data is
   stored under `C:\Users\<you>\.tokenomy\` (in the `data\` and `config\`
   subfolders). **Close the window to quit.**
4. When a new version ships, the dashboard shows an update banner — click it,
   download the new `Tokenomy.exe`, and overwrite the old one.

## Quick start (developer — from source)

```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json   # then edit your budget
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

On Windows, double-click `start_tokenomy.bat` (ingest → dashboard → opens browser).

## Configure your budget

Edit `config/tokenomy.config.json`, or use the **Settings** page in the
dashboard (`/settings`):

```json
{
  "user_label": "me",
  "budget": { "claude": 100, "codex": 50 },
  "pricing_overrides": {}
}
```

- `budget.claude` / `budget.codex`: your monthly cap in USD. `0` = no cap
  (usage-only tracking).
- `pricing_overrides`: override per-model rates if your billing differs from
  public list prices, e.g. `{"opus": {"input": 9.0, "output": 36.0}}`.

## Data sources

- Claude Code: `~/.claude/projects/**/*.jsonl` (per-message usage + cache).
- Codex CLI: `~/.codex/sessions/**/rollout-*.jsonl` (per-session cumulative).

## Pricing

`config/pricing.json` ships with public API list prices. Update them as
providers change prices, or override per-user via `pricing_overrides`.

## Adding a parser for another tool

Tokenomy normalizes each tool's logs into `UsageRecord` (see
`tokenomy/parser.py`). To support another CLI, write a module that discovers
its log files and yields `UsageRecord`s, then ingest them via
`tokenomy.db.ingest_records(conn, records, pricing)` — see
`tokenomy/codex_parser.py` as a reference implementation.

## License

MIT — see [LICENSE](LICENSE).
