"""
eskom_client.py
----------------
Live integration with the EskomSePush (ESP) API.
https://developer.sepush.co.za/business/2.0

TOKEN:        Set ESP_API_TOKEN in .env
FREE TIER:    50 requests/day — /status and /api_allowance endpoints
PAID TIER:    Unlocks /areas_search and /area for suburb-level schedules

CACHING: Successful /status responses are cached for ESP_CACHE_TTL_SECONDS
(default 1800 = 30 min) to protect the 50 req/day free-tier limit.

RESULT TYPES
------------
Every public function returns a dict with a "result_type" key:
    RT_OK           — success; check other keys for data
    RT_RATE_LIMITED — 429; daily request limit exhausted
    RT_ERROR        — any other failure (timeout, bad token, network, etc.)
    RT_NOT_CONFIGURED — ESP_API_TOKEN not set
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger("connectco.eskom_client")

ESP_BASE_URL   = "https://developer.sepush.co.za/business/2.0"
REQUEST_TIMEOUT = 8

# Result type constants
RT_OK             = "ok"
RT_RATE_LIMITED   = "rate_limited"
RT_NOT_CONFIGURED = "not_configured"
RT_ERROR          = "error"

# In-memory TTL cache: key -> (data_dict, unix_timestamp)
_cache: Dict[str, tuple] = {}
_CACHE_TTL: int = int(os.environ.get("ESP_CACHE_TTL_SECONDS", "1800"))

# Municipalities that have their own independent loadshedding stage
# distinct from the national Eskom stage, per the ESP /status response.
MUNICIPAL_OVERRIDES = {
    "City of Cape Town": "capetown",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Returns True if ESP_API_TOKEN is set in the environment."""
    return bool(os.environ.get("ESP_API_TOKEN", "").strip())


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> Optional[Dict]:
    if key in _cache:
        value, ts = _cache[key]
        age = time.time() - ts
        if age < _CACHE_TTL:
            logger.debug("ESP cache hit '%s' (age %.0fs / TTL %ds)", key, age, _CACHE_TTL)
            return value
        del _cache[key]
    return None


def _cache_set(key: str, value: Dict) -> None:
    _cache[key] = (value, time.time())


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _request(path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Authenticated GET to the ESP API. Returns a result-type dict, never raises.
    """
    token = os.environ.get("ESP_API_TOKEN", "").strip()
    if not token:
        logger.error("ESP_API_TOKEN is not set.")
        return {"result_type": RT_NOT_CONFIGURED}

    try:
        r = requests.get(
            f"{ESP_BASE_URL}{path}",
            headers={"token": token},
            params=params or {},
            timeout=REQUEST_TIMEOUT,
        )

        if r.status_code == 429:
            logger.warning(
                "ESP rate limit hit (429). Daily free-tier limit of 50 requests exhausted. "
                "Cache TTL is %ds — reduce request frequency or upgrade to a paid plan.",
                _CACHE_TTL,
            )
            return {"result_type": RT_RATE_LIMITED}

        if r.status_code == 403:
            logger.error(
                "ESP 403 Forbidden — token may be invalid. Token prefix: %s...", token[:8]
            )
            return {"result_type": RT_ERROR, "message": "Invalid or suspended API token."}

        if r.status_code == 410:
            logger.warning("ESP 410 Gone for %s — endpoint requires a paid plan.", path)
            return {"result_type": RT_ERROR, "message": "Endpoint requires a paid ESP plan."}

        if not r.ok:
            logger.warning("ESP %s → HTTP %d: %s", path, r.status_code, r.text[:250])
            return {"result_type": RT_ERROR, "message": f"Unexpected HTTP {r.status_code}."}

        return {"result_type": RT_OK, "data": r.json()}

    except requests.Timeout:
        logger.warning("ESP request to %s timed out (%ds).", path, REQUEST_TIMEOUT)
        return {"result_type": RT_ERROR, "message": "Request to ESP API timed out."}
    except requests.ConnectionError:
        logger.warning("ESP request to %s — connection error.", path)
        return {"result_type": RT_ERROR, "message": "Could not connect to ESP API."}
    except requests.RequestException as exc:
        logger.warning("ESP request to %s raised %s: %s", path, type(exc).__name__, exc)
        return {"result_type": RT_ERROR, "message": str(exc)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_allowance() -> Optional[Dict[str, Any]]:
    """
    Returns current API token usage from the ESP /api_allowance endpoint.

    Response shape on success:
        {
            "count": int,   # requests used today
            "limit": int,   # daily maximum (50 on free tier)
            "type":  str,   # "daily"
        }
    Returns None if the request fails or token is not configured.
    """
    result = _request("/api_allowance")
    if result["result_type"] != RT_OK:
        return None
    return result["data"].get("allowance")


def get_status_for_municipality(municipality: str) -> Dict[str, Any]:
    """
    Returns current loadshedding status relevant to the given municipality.
    Uses the /status endpoint (free tier, cached).

    Success shape:
        {
            "result_type":    "ok",
            "stage":          int,       # 0 = no loadshedding
            "stage_starts_at": datetime, # tz-aware; None if unknown
            "is_active_now":  bool,
            "region_label":   str,
        }

    Failure shapes:
        {"result_type": "rate_limited"}
        {"result_type": "not_configured"}
        {"result_type": "error", "message": str}
    """
    cached = _cache_get("status")
    if cached is not None:
        return _parse_status(cached, municipality)

    result = _request("/status")
    if result["result_type"] != RT_OK:
        return result  # propagate rate_limited / error / not_configured

    raw = result["data"]
    _cache_set("status", raw)
    return _parse_status(raw, municipality)


def get_suburb_schedule(suburb: str) -> Dict[str, Any]:
    """
    Suburb-level loadshedding schedule via /areas_search + /area.
    PAID TIER ONLY — returns RT_ERROR on free tier (410 Gone).
    """
    search = _request("/areas_search", params={"text": suburb})
    if search["result_type"] != RT_OK:
        return search

    areas = search["data"].get("areas", [])
    if not areas:
        return {"result_type": RT_ERROR, "message": f"No ESP area found for '{suburb}'."}

    area_id = areas[0].get("id")
    if not area_id:
        return {"result_type": RT_ERROR, "message": "ESP area result had no id field."}

    return _request("/area", params={"id": area_id})


# ---------------------------------------------------------------------------
# Internal parsing
# ---------------------------------------------------------------------------

def _parse_status(data: Dict, municipality: str) -> Dict[str, Any]:
    """Extract municipality-relevant loadshedding stage from raw /status response."""
    status_key   = MUNICIPAL_OVERRIDES.get(municipality, "eskom")
    status_block = data.get("status", {})
    region       = status_block.get(status_key) or status_block.get("eskom")

    if not region:
        return {"result_type": RT_ERROR, "message": "Unexpected shape in ESP /status response."}

    next_stages = region.get("next_stages") or []
    if not next_stages:
        return {
            "result_type":    RT_OK,
            "stage":          0,
            "stage_starts_at": None,
            "is_active_now":  False,
            "region_label":   region.get("name", status_key),
        }

    current = next_stages[0]
    try:
        stage = int(current.get("stage", 0))
    except (TypeError, ValueError):
        stage = 0

    starts_at = _parse_iso(current.get("stage_start_timestamp"))
    now       = datetime.now(timezone.utc)
    is_active = stage > 0 and starts_at is not None and starts_at <= now

    return {
        "result_type":     RT_OK,
        "stage":           stage,
        "stage_starts_at": starts_at,
        "is_active_now":   is_active,
        "region_label":    region.get("name", status_key),
    }


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
