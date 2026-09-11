"""
ethekwini_client.py
--------------------
Live integration for eThekwini Municipality (Durban) electricity outages.

DATA SOURCE
-----------
Discovered by inspecting the Network tab on:
    https://webfaults.durban.gov.za/WebsiteFaultsEllipseProd/Outage

The page uses jQuery DataTables with server-side processing. On load,
two AJAX POST requests are made to populate the two outage tables:

    POST /WebsiteFaultsEllipseProd/Outage/openIndex
        Medium voltage faults currently being diagnosed.
        Columns: Fault number | Area | Depot

    POST /WebsiteFaultsEllipseProd/Outage/inworkIndex
        Faults escalated to depot for repair (more complex).
        Columns: Fault number | Area | Depot | Restored Date |
                 Restored Time | % Restored | Primary Fault | Secondary Fault

DATATABLES RESPONSE FORMAT
---------------------------
Both endpoints return standard DataTables server-side JSON:
    {
        "draw":             int,
        "recordsTotal":     int,
        "recordsFiltered":  int,
        "data": [
            ["FLT-001", "UMHLANGA", "PINETOWN DEPOT"],
            ...
        ]
    }
Each row in "data" is an array of strings matching the column order above.

COLUMN INDICES
--------------
openIndex:
    0 = Fault number
    1 = Area
    2 = Depot

inworkIndex:
    0 = Fault number
    1 = Area
    2 = Depot
    3 = Restored Date   (e.g. "01/07/2026")
    4 = Restored Time   (e.g. "14:00")
    5 = % Restored
    6 = Primary Fault
    7 = Secondary Fault

PRIORITY
--------
openIndex is checked first (active faults being diagnosed — higher urgency).
inworkIndex is checked second (repairs in progress — has restoration time).

CACHING
-------
Results cached for ETHEKWINI_CACHE_TTL_SECONDS (default 600 = 10 min).

SUBURB MATCHING
---------------
Matches the customer's suburb against the Area column (index 1),
case-insensitively. eThekwini uses area names like "UMHLANGA",
"MORNINGSIDE", "PINETOWN", so we do containment matching in both
directions to handle capitalisation and partial names.
"""

import os
import re
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("connectco.ethekwini_client")

_BASE_URL    = "https://webfaults.durban.gov.za/WebsiteFaultsEllipseProd/Outage"
_OPEN_URL    = f"{_BASE_URL}/openIndex"
_INWORK_URL  = f"{_BASE_URL}/inworkIndex"
_TIMEOUT     = 10

# Standard DataTables server-side POST body — fetch all rows in one call
_DT_PARAMS = {
    "draw":               "1",
    "start":              "0",
    "length":             "1000",   # large enough to get everything
    "search[value]":      "",
    "search[regex]":      "false",
    "order[0][column]":   "1",      # order by Area
    "order[0][dir]":      "asc",
}

_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type":     "application/x-www-form-urlencoded",
    "Referer":          f"{_BASE_URL}",
}

# In-memory TTL cache: url -> (data_list, unix_timestamp)
_cache: Dict[str, tuple] = {}
_CACHE_TTL: int = int(os.environ.get("ETHEKWINI_CACHE_TTL_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_electricity_outage(suburb: str) -> Optional[Dict[str, Any]]:
    """
    Returns the most relevant active electricity outage for the given
    suburb, or None if nothing is found or the feed is unavailable.

    Return shape (on match):
        {
            "status":            "Power Outage",
            "source":            str,
            "started_at":        None,         # not provided by eThekwini
            "estimated_restore": datetime | None,
            "message":           str,
        }
    """
    # openIndex — active faults being diagnosed (higher urgency, check first)
    open_rows = _fetch(_OPEN_URL)
    if open_rows is not None:
        row = _find_match(open_rows, suburb)
        if row:
            logger.info(
                "eThekwini OPEN fault match for suburb '%s': %s",
                suburb, row
            )
            return _format_open(row)

    # inworkIndex — repairs in progress (has estimated restoration time)
    inwork_rows = _fetch(_INWORK_URL)
    if inwork_rows is not None:
        row = _find_match(inwork_rows, suburb)
        if row:
            logger.info(
                "eThekwini IN-WORK fault match for suburb '%s': %s",
                suburb, row
            )
            return _format_inwork(row)

    return None


# ---------------------------------------------------------------------------
# Feed fetching with cache
# ---------------------------------------------------------------------------

def _fetch(url: str) -> Optional[List[List[str]]]:
    """
    POST to the DataTables endpoint and return the list of data rows,
    or None on any failure. Results are cached for _CACHE_TTL seconds.
    """
    if url in _cache:
        rows, ts = _cache[url]
        age = time.time() - ts
        if age < _CACHE_TTL:
            logger.debug("eThekwini cache hit '%s' (age %.0fs)", url, age)
            return rows
        del _cache[url]

    try:
        r = requests.post(
            url,
            data=_DT_PARAMS,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()

        # Guard against JSON null or a non-dict response (e.g. empty array)
        if not isinstance(payload, dict):
            logger.warning(
                "eThekwini: unexpected response type %s from %s",
                type(payload).__name__, url,
            )
            return None

        rows = payload.get("data") or []
        _cache[url] = (rows, time.time())
        logger.debug("eThekwini fetched %d rows from %s", len(rows), url)
        return rows

    except requests.RequestException as exc:
        logger.warning("eThekwini fetch failed for %s: %s", url, exc)
        return None
    except (ValueError, KeyError, AttributeError, TypeError) as exc:
        logger.warning("eThekwini response parse error for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Suburb matching
# ---------------------------------------------------------------------------

def _find_match(rows: List, suburb: str) -> Optional[Any]:
    """
    Case-insensitive containment match on the Area column.
    Handles both DataTables row formats:
      - Array:  ["FLT-001", "UMHLANGA", "PINETOWN DEPOT"]
      - Object: {"FaultNumber": "FLT-001", "Area": "UMHLANGA", "Depot": "..."}
    Returns the first matching row, or None.
    """
    if not rows:
        return None

    suburb_norm = suburb.upper().strip()
    first = rows[0]

    if isinstance(first, dict):
        # Log the actual keys once so we can verify/tune if needed
        logger.debug("eThekwini row is dict. Keys: %s", list(first.keys()))
        area_key = _detect_area_key(first)
        if area_key is None:
            logger.warning(
                "eThekwini: cannot identify area column. Raw first row: %s", first
            )
            return None
        for row in rows:
            area_norm = (row.get(area_key) or "").upper().strip()
            if suburb_norm in area_norm or area_norm in suburb_norm:
                return row

    else:
        # Original assumption: list/array rows
        for row in rows:
            if len(row) < 2:
                continue
            area_norm = (_safe_cell(row, 1) or "").upper().strip()
            if suburb_norm in area_norm or area_norm in suburb_norm:
                return row

    return None


def _detect_area_key(row: dict) -> Optional[str]:
    """
    Identify which key in a dict row holds the Area/suburb value.
    Tries common DataTables column naming conventions in priority order.
    """
    candidates = [
        "Area", "area", "AREA",
        "AreaName", "area_name", "areaName",
        "Suburb", "suburb",
        "Location", "location",
        "Description", "description",
    ]
    for key in candidates:
        if key in row:
            return key
    return None


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _format_open(row: Any) -> Dict[str, Any]:
    """
    Format an openIndex row (fault being diagnosed — no restoration time yet).
    Array columns: [0] Fault number | [1] Area | [2] Depot
    Dict keys:     FaultNumber/fault_number | Area/area | Depot/depot
    """
    fault_ref = _get(row, 0, ["FaultNumber", "fault_number", "Fault", "FaultNo", "Reference"])
    area      = _get(row, 1, ["Area", "area", "AreaName", "Suburb"])
    depot     = _get(row, 2, ["Depot", "depot", "DepotName"])

    area  = area.title()  if area  else "your area"
    depot = depot.title() if depot else ""

    ref_note   = f" (ref: {fault_ref})" if fault_ref else ""
    depot_note = f" Depot: {depot}."    if depot     else ""

    return {
        "status":            "Power Outage",
        "source":            "eThekwini Municipality — webfaults.durban.gov.za (live)",
        "started_at":        None,
        "estimated_restore": None,
        "message": (
            f"A medium-voltage electricity fault has been reported in {area} "
            f"and is being diagnosed by eThekwini fault teams{ref_note}.{depot_note} "
            f"Restoration is typically within 1–8 hours."
        ),
    }


def _format_inwork(row: Any) -> Dict[str, Any]:
    """
    Format an inworkIndex row (repair escalated — has restoration date/time).
    Array cols: [0] Fault | [1] Area | [2] Depot | [3] Date | [4] Time |
                [5] % Restored | [6] Primary Fault | [7] Secondary Fault
    Dict keys:  FaultNumber | Area | Depot | RestoredDate | RestoredTime |
                PercentRestored | PrimaryFault | SecondaryFault  (approximate)
    """
    fault_ref      = _get(row, 0, ["FaultNumber", "fault_number", "Fault", "FaultNo"])
    area           = _get(row, 1, ["Area", "area", "AreaName"])
    depot          = _get(row, 2, ["Depot", "depot"])
    restored_date  = _get(row, 3, ["RestoredDate", "restored_date", "RestoreDate", "Date"])
    restored_time  = _get(row, 4, ["RestoredTime", "restored_time", "RestoreTime", "Time"])
    pct_restored   = _get(row, 5, ["PercentRestored", "percent_restored", "Restored", "PercRestored"])
    primary_fault  = _get(row, 6, ["PrimaryFault", "primary_fault", "FaultType", "Primary"])

    area  = area.title()  if area  else "your area"
    depot = depot.title() if depot else ""

    estimated_restore = _parse_restore_datetime(restored_date, restored_time)

    ref_note   = f" (ref: {fault_ref})"          if fault_ref else ""
    depot_note = f" Depot: {depot}."              if depot     else ""
    pct_note   = f" {pct_restored}% restored."   if pct_restored and pct_restored not in ("0", "0%", "") else ""
    fault_note = f" Fault type: {primary_fault}." if primary_fault else ""

    return {
        "status":            "Power Outage",
        "source":            "eThekwini Municipality — webfaults.durban.gov.za (live)",
        "started_at":        None,
        "estimated_restore": estimated_restore,
        "message": (
            f"An electricity fault in {area} has been escalated for repair{ref_note}."
            f"{fault_note}{pct_note}{depot_note} "
            f"Complex repairs may take 24–48 hours to complete."
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(row: Any, list_index: int, dict_keys: List[str]) -> str:
    """
    Safely extract a cell value from either a list row (by index) or a
    dict row (by trying each key in dict_keys in order).
    Always returns a clean string with HTML tags stripped.
    """
    raw = ""
    if isinstance(row, dict):
        for key in dict_keys:
            if key in row and row[key] is not None:
                raw = str(row[key])
                break
    else:
        raw = _safe_cell(row, list_index)
    return re.sub(r"<[^>]+>", "", raw).strip()


def _safe_cell(row: Any, index: int) -> str:
    """Safely get index from a list/tuple row; returns '' on out-of-bounds."""
    try:
        val = row[index]
        return str(val) if val is not None else ""
    except (IndexError, KeyError, TypeError):
        return ""


def _parse_restore_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """
    Parses eThekwini's restoration date ("01/07/2026") and time ("14:00")
    into a tz-aware UTC datetime. Returns None if unparseable.

    NOTE: eThekwini is in SAST (UTC+2). The datetime is stored as-is without
    explicit timezone info in the table, so we treat it as SAST and convert.
    """
    if not date_str or not time_str:
        return None
    try:
        from datetime import timedelta
        naive = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
        # SAST = UTC + 2
        sast_offset = timedelta(hours=2)
        return naive.replace(tzinfo=timezone.utc) - sast_offset + sast_offset
    except ValueError:
        try:
            # Try alternate date format just in case
            naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            return naive.replace(tzinfo=timezone(offset=__import__('datetime').timedelta(hours=2)))
        except ValueError:
            logger.warning("eThekwini: could not parse restoration datetime '%s %s'", date_str, time_str)
            return None
