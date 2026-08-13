# Carbitrage

Luxury EV market tracker for arbitrage hunting. Tracks Porsche, Mercedes, Lucid, BMW, and Audi EVs across the used market, records daily price history, and flags deals.

## Features

- **Watch-based tracking** — define saved searches by make/model/year/price/radius
- **Trim filter** — client-side substring filter for models where EVs hide as trims (e.g., Cayenne "Electric")
- **API budget gauge** — hard stop at 950/1000 free calls/month, impossible to incur a charge
- **Price history** — daily snapshots per VIN; see how long cars sit and how far prices drop
- **Sold detection** — vehicles unseen for 7 days are marked sold/delisted
- **Manual URL tracking** — for broker sites, Bring a Trailer, private sales the API doesn't carry
- **Star/flag** vehicles of interest
- **CSV export** — active, starred, or sold views
- **One-click seed** — pre-populates 9 recommended luxury EV watches

## Quick Start (Replit)

1. Import this repo into Replit (Python template)
2. Add Secrets (padlock icon in sidebar):
   - `AUTODEV_API_KEY` — free key from [auto.dev/pricing](https://auto.dev/pricing)
   - `APP_PASSWORD` — (optional) set to require a password to access the UI
3. Click Run
4. Click "Load recommended luxury EV watches" then "Run index now"

## Quick Start (Local)

```bash
pip install flask requests
python main.py
# Open http://localhost:8080
```

Set the API key as an environment variable:
```bash
# Windows PowerShell
$env:AUTODEV_API_KEY = "your_key_here"

# Linux/Mac
export AUTODEV_API_KEY="your_key_here"
```

## Architecture

| File | Purpose |
|------|---------|
| `main.py` | Flask web app, all routes |
| `db.py` | SQLite schema + data access layer |
| `api_client.py` | Auto.dev API client with budget guard |
| `indexer.py` | Daily run orchestrator (watches + manual URLs + sold detection) |
| `manual_tracker.py` | URL fetcher for non-API listings |
| `templates/` | Jinja2 HTML templates (dark theme dashboard) |

## API Budget

The free Auto.dev tier gives 1,000 calls/month. The app hard-stops at 950 to leave margin. A full run of 9 watches uses ~25 calls. You can safely run daily without hitting the limit.

## Coming Soon

- Porsche Finder scraper (confirmed accessible, JSON API)
- Mercedes CPO site scraper
