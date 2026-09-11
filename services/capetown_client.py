"""
capetown_client.py
-------------------
Live integration for City of Cape Town electricity fault and maintenance data.

DATA SOURCE
-----------
These two JSON endpoints are what the official Cape Town service alerts
page (capetown.gov.za/City-Alerts.aspx) fetches from at runtime. They
were discovered by inspecting the page's Network tab in Chrome DevTools —
the page itself throws a CORS error when the browser tries to call them
cross-origin, but that restriction does not apply to server-side Python
requests, which is why our Flask backend can call them freely.

    Unplanned faults:
    https://service-alerts.cct-datascience.xyz/coct-service_alerts-current-unplanned.json

    Planned maintenance:
    https://service-alerts.cct-datascience.xyz/coct-service_alerts-current-planned.json

JSON SCHEMA (per alert object)
-------------------------------
{
    "Id":                    int,
    "service_area":          str,   # "Electricity" | "Water & Sanitation" | "Roads & Stormwater" | …
    "title":                 str,   # e.g. "Unplanned Maintenance - Feeder cable"
    "description":           str,
    "area":                  str,   # Broad district/ward name, e.g. "Mowbray", "SEA POINT"
    "location":              str,   # Semicolon-separated suburb list, e.g. "WOODSTOCK;OBSERVATORY;"
    "publish_date":          str,   # ISO-8601
    "effective_date":        str,   # ISO-8601 — when the alert becomes relevant
    "expiry_date":           str,   # ISO-8601 — when the alert expires/is removed
    "start_timestamp":       str,   # ISO-8601 — actual outage start
    "forecast_end_timestamp":str | null,  # Estimated restoration (often null)
    "planned":               bool,
    "request_number":        str | null
}

We filter exclusively to service_area == "Electricity" since this tool
is scoped to power-related internet outages. All other service areas
(water mains, roads, refuse) are ignored.

CACHING
-------
Both feeds are cached in memory for CCT_CACHE_TTL_SECONDS (default 600 = 10 min).
The CCT data science server updates these files periodically; 10 minutes
is a reasonable balance between freshness and server load.

SUBURB MATCHING
---------------
We match the customer's suburb against each alert's `area` (broad district)
and each semicolon-separated token in `location` (specific suburbs),
case-insensitively. We also do partial/containment matching to handle
variations like "Hout Bay" vs "HOUT BAY" and "Sea Point" vs "SEA POINT".
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("connectco.capetown_client")

_UNPLANNED_URL = "https://service-alerts.cct-datascience.xyz/coct-service_alerts-current-unplanned.json"
_PLANNED_URL   = "https://service-alerts.cct-datascience.xyz/coct-service_alerts-current-planned.json"
_TIMEOUT       = 8  # seconds

_ELECTRICITY = "Electricity"

# In-memory TTL cache: url -> (data, unix_timestamp)
_cache: Dict[str, tuple] = {}
_CACHE_TTL: int = int(os.environ.get("CCT_CACHE_TTL_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_electricity_outage(suburb: str) -> Optional[Dict[str, Any]]:
    """
    Returns the most relevant active electricity alert for the given suburb,
    or None if no electricity alert is found or if the feed is unavailable.

    Priority: unplanned fault > planned maintenance.
    (An unplanned fault is more likely to be causing the customer's outage
    than scheduled maintenance, so we check it first.)

    Return shape (on match):
        {
            "status":            "Power Outage" | "Planned Maintenance",
            "source":            str,
            "started_at":        datetime,     # tz-aware UTC
            "estimated_restore": datetime | None,
            "message":           str,
        }
    """
    # Unplanned faults — highest priority
    unplanned_feed = _fetch(_UNPLANNED_URL)
    if unplanned_feed is not None:
        alert = _find_electricity_match(unplanned_feed, suburb)
        if alert:
            logger.info(
                "Cape Town UNPLANNED electricity match for suburb '%s': [%s] %s",
                suburb, alert.get("Id"), alert.get("title"),
            )
            return _format_alert(alert, planned=False)

    # Planned maintenance — lower priority
    planned_feed = _fetch(_PLANNED_URL)
    if planned_feed is not None:
        alert = _find_electricity_match(planned_feed, suburb)
        if alert:
            logger.info(
                "Cape Town PLANNED electricity match for suburb '%s': [%s] %s",
                suburb, alert.get("Id"), alert.get("title"),
            )
            return _format_alert(alert, planned=True)

    return None


# ---------------------------------------------------------------------------
# Feed fetching with cache
# ---------------------------------------------------------------------------

def _fetch(url: str) -> Optional[List[Dict]]:
    """
    Returns parsed JSON list from the given URL, using the in-memory TTL
    cache. Returns None on any network/HTTP failure (logged at WARNING).
    """
    if url in _cache:
        data, ts = _cache[url]
        age = time.time() - ts
        if age < _CACHE_TTL:
            logger.debug("CCT cache hit '%s' (age %.0fs / TTL %ds)", url, age, _CACHE_TTL)
            return data
        del _cache[url]

    try:
        r = requests.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        # Guard against JSON null or non-list response
        if not isinstance(data, list):
            logger.warning(
                "CCT: unexpected response type %s from %s",
                type(data).__name__, url,
            )
            return None

        _cache[url] = (data, time.time())
        logger.debug("CCT fetched %d alerts from %s", len(data), url)
        return data
    except requests.RequestException as exc:
        logger.warning("CCT alerts fetch failed for %s: %s", url, exc)
        return None
    except (ValueError, AttributeError, TypeError) as exc:
        logger.warning("CCT response parse error for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Suburb matching
# ---------------------------------------------------------------------------

def _find_electricity_match(
    alerts: List[Dict], suburb: str
) -> Optional[Dict]:
    """
    Scan alerts for one where service_area is "Electricity" and the
    customer's suburb appears in the alert's area or location field.

    Matching is case-insensitive. We check for containment in both
    directions (suburb in alert token, and alert token in suburb) to
    handle common variations:
        "Sea Point"  ↔  "SEA POINT"
        "Hout Bay"   ↔  "HOUT BAY"
        "Observatory" ↔ "OBSERVATORY"

    Returns the first match found (alerts come pre-sorted by recency
    from the CCT data science server, so first = most recent).
    """
    suburb_norm = suburb.upper().strip()

    for alert in alerts:
        if alert.get("service_area") != _ELECTRICITY:
            continue

        # 1. Check broad area/district name
        area_norm = (alert.get("area") or "").upper().strip()
        if _tokens_overlap(suburb_norm, area_norm):
            return alert

        # 2. Check each semicolon-separated location token
        location_raw = alert.get("location") or ""
        for token in location_raw.split(";"):
            token_norm = token.upper().strip()
            if token_norm and _tokens_overlap(suburb_norm, token_norm):
                return alert

    return None


def _tokens_overlap(a: str, b: str) -> bool:
    """True if either string is contained in the other (case already normalised)."""
    return bool(a and b and (a in b or b in a))


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _format_alert(alert: Dict, planned: bool) -> Dict[str, Any]:
    status = "Planned Maintenance" if planned else "Power Outage"

    started_at       = _parse_iso(alert.get("start_timestamp"))
    estimated_restore = _parse_iso(alert.get("forecast_end_timestamp"))

    # Build a clean location string from the semicolon-separated field
    raw_location = alert.get("location") or ""
    location_parts = [p.strip() for p in raw_location.split(";") if p.strip()]
    location_str = ", ".join(p.title() for p in location_parts)

    area = (alert.get("area") or "").title()
    title = alert.get("title") or status
    description = alert.get("description") or ""

    # Build a readable message, appending location detail when available
    location_detail = ""
    if location_str and location_str.upper() != area.upper():
        location_detail = f" Affected areas: {location_str}."
    elif area:
        location_detail = f" Area: {area}."

    message = f"{title}. {description}{location_detail}".strip()

    return {
        "status":            status,
        "source":            "City of Cape Town Electricity (live)",
        "started_at":        started_at,
        "estimated_restore": estimated_restore,
        "message":           message,
    }


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
