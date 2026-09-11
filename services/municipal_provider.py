"""
municipal_provider.py
----------------------
Dispatcher for municipal electricity fault and maintenance data.
Each municipality gets its own dedicated client module; this file
routes the request to the correct one based on the municipality name.

INTEGRATED MUNICIPALITIES
--------------------------
    City of Cape Town  → services/capetown_client.py
                         Source: cct-datascience.xyz JSON feeds
                         (the hidden API behind capetown.gov.za/City-Alerts.aspx)

PENDING MUNICIPALITIES
-----------------------
    City of Johannesburg (City Power) — no public API found yet
    City of Tshwane                   — no public API found yet
    eThekwini (Durban)                — no public API found yet
    Nelson Mandela Bay                — no public API found yet
    Buffalo City                      — no public API found yet

    Next step for each: open their service-alerts / outage page in
    Chrome DevTools → Network → Fetch/XHR and look for a hidden JSON
    endpoint (same approach that found the CCT one). If none exists,
    fall back to HTML scraping with BeautifulSoup.

INTEGRATION CONTRACT
---------------------
get_municipal_outage() returns either None (no issue / not integrated)
or a dict:
    {
        "status":            "Power Outage" | "Planned Maintenance",
        "source":            str,
        "started_at":        datetime,      # tz-aware
        "estimated_restore": datetime | None,
        "message":           str,
    }
"""

from typing import Optional, Dict, Any
from services import capetown_client, ethekwini_client, centlec_client

INTEGRATED = {
    "City of Cape Town",
    "eThekwini Metropolitan Municipality",
    "Mangaung Metropolitan Municipality",
}

def is_integrated(municipality: str) -> bool:
    return municipality in INTEGRATED


def get_municipal_outage(municipality: str, suburb: str) -> Optional[Dict[str, Any]]:
    if municipality == "City of Cape Town":
        return capetown_client.get_electricity_outage(suburb)

    if municipality == "eThekwini Metropolitan Municipality":
        return ethekwini_client.get_electricity_outage(suburb)

    if municipality == "Mangaung Metropolitan Municipality":
        return centlec_client.get_electricity_outage(suburb)

    # ── Pending ───────────────────────────────────────────────────────────
    # City of Johannesburg (City Power): internal server, not publicly accessible
    # City of Tshwane: WordPress page, no data endpoints found
    # Nelson Mandela Bay: static schedule page only
    # ─────────────────────────────────────────────────────────────────────
    return None
