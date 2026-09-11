# ConnectCo Power Status Checker (POC)

A customer-facing proof-of-concept web app for **ConnectCo**. It helps customers
determine whether an internet outage is likely caused by scheduled loadshedding,
a municipal power outage, planned maintenance, or whether there's no reported
power issue (in which case they should contact support) — and approximately
when the issue started.

## What's actually real here

This matters, so it's worth being explicit:

| Data | Status | Source |
|---|---|---|
| Loadshedding | **Real**, when configured | [EskomSePush (ESP) API](https://eskomsepush.gumroad.com/l/api) — the de-facto standard third-party API for SA loadshedding data, since Eskom itself has no public API |
| Municipal faults / planned maintenance | **Placeholder** | No South African municipality publishes a public API for this (Cape Town, City Power Joburg, Tshwane, eThekwini etc. all only have human-facing webpages/maps). See `services/municipal_provider.py` for the real options going forward (manual ops curation, a licensed data feed, or a maintained per-municipality scraper). |
| "No issues" fallback | Real (by elimination) | Internal network monitoring would plug in here |

Every API response includes a `dataSource` field (`"live"` or `"demo"`) and the
UI shows a small **Live Data** / **Demo Data** badge on the result card, so
it's always clear which is which — never silently fake.

## Project structure

```
power-status-checker/
├── app.py                       # Flask app + routes
├── requirements.txt
├── .env.example                 # Copy to .env and add your ESP token
├── services/
│   ├── eskom_client.py          # Real EskomSePush (ESP) API integration
│   ├── municipal_provider.py    # Municipal fault/maintenance adapter (placeholder + docs on real options)
│   └── power_service.py         # Orchestrates the above into one response
├── templates/
│   └── index.html               # Dashboard UI
└── static/
    ├── style.css                  # ConnectCo styling (white / light-blue / grey)
    └── script.js                  # Dropdowns + API calls + result rendering
```

## Running locally

```bash
# 1. Create and activate a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional but recommended) Configure a real ESP API token
cp .env.example .env
# edit .env and paste in your token from https://eskomsepush.gumroad.com/l/api

# 4. Run the app
python app.py
```

Then open **http://localhost:5000** in your browser.

Without a token in `.env`, the app still runs fully — loadshedding falls back
to deterministic demo data so every status type can still be demonstrated.

### Getting an ESP API token

1. Request one (free tier available) at https://eskomsepush.gumroad.com/l/api
2. Free tier: 50 requests/day, and only the `/status` endpoint (national
   Eskom stage + Cape Town's independent municipal stage). This is what
   `eskom_client.get_status_for_municipality()` uses.
3. Paid tier: unlocks `/areas_search` and `/area`, which give exact
   suburb-level schedules instead of a municipality-wide estimate. The code
   for this already exists in `eskom_client.get_suburb_schedule()` — it's
   just not wired into the main flow yet, since it requires a paid token to
   test against.

## API

`GET /api/power-status?province=<>&municipality=<>&suburb=<>`

Returns JSON, e.g.:

```json
{
    "status": "Power Outage",
    "source": "City of Cape Town (demo data — no live municipal feed configured)",
    "startedAt": "2026-06-30T14:05:00+00:00",
    "estimatedRestore": "2026-06-30T18:30:00+00:00",
    "planned": false,
    "message": "A municipal electricity outage has been reported for your area.",
    "dataSource": "demo"
}
```

`status` is always one of: `"No Reported Issues"`, `"Planned Maintenance"`,
`"Loadshedding"`, `"Power Outage"`. `startedAt` and `estimatedRestore` are
ISO-8601 timestamps (or `null`), formatted client-side into local time plus a
relative description (e.g. "14:05 (started 1h 40m ago)").

## Replacing the municipal placeholder with real data

All of this logic lives in `services/municipal_provider.py` — read the
module docstring first, it lays out the three realistic paths (manual ops
curation, a licensed aggregator, or maintained per-municipality scrapers)
along with the trade-offs of each. Whichever you choose, `get_municipal_outage()`
should keep the same return shape so nothing else in the app needs to change.
