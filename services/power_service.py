"""
power_service.py
-----------------
Orchestration layer — checks loadshedding (ESP) and municipal outage data
independently, then merges them into a single response.

KEY DESIGN PRINCIPLE: ESP and municipal data are completely independent.
A rate-limited or failed ESP request must NEVER block the municipal check —
Cape Town, eThekwini and Centlec data should still be returned even if the
loadshedding API is unavailable.

OUTCOME STATES (in priority order)
-------------------------------------
1. Loadshedding           — ESP confirms active stage > 0
2. Power Outage           — Municipal client reports an unplanned fault
3. Planned Maintenance    — Municipal client reports scheduled work
4. No Reported Issues     — All checked sources confirm no issue
                            (with a note if any source was unavailable)
5. Data Unavailable       — No sources could be reached at all
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

from services import eskom_client, municipal_provider
from services.eskom_client import RT_OK, RT_RATE_LIMITED, RT_NOT_CONFIGURED, RT_ERROR


def get_power_status(province: str, municipality: str, suburb: str) -> Dict[str, Any]:
    """
    Main entry point. Always returns a dict in the API contract shape:
        {
            "status":           str,
            "source":           str,
            "startedAt":        str | None,
            "estimatedRestore": str | None,
            "planned":          bool,
            "message":          str,
            "dataSource":       "live" | "partial" | "unavailable",
            "reason":           str    (only when dataSource == "unavailable")
        }
    """

    # ── 1. Check Eskom loadshedding via ESP ──────────────────────────────
    # This may succeed, fail, or be rate-limited independently of the
    # municipal check below. We capture the outcome and continue regardless.
    esp       = eskom_client.get_status_for_municipality(municipality)
    esp_ok    = esp["result_type"] == RT_OK
    esp_issue = esp["result_type"] if not esp_ok else None   # None means success

    # If loadshedding is actively detected, return immediately —
    # this is the most likely cause of an internet outage and we don't
    # need to check municipal data to determine the primary issue.
    if esp_ok and esp["is_active_now"] and esp["stage"] > 0:
        starts_at         = esp["stage_starts_at"]
        estimated_restore = starts_at + timedelta(hours=2) if starts_at else None
        return _result(
            status=f"Loadshedding",
            source=f"EskomSePush (ESP) — Stage {esp['stage']}, {esp['region_label']}",
            started_at=starts_at,
            estimated_restore=estimated_restore,
            planned=True,
            message=(
                f"Your area is currently in a Stage {esp['stage']} loadshedding slot. "
                "Internet connectivity may be affected until power is restored at the "
                "end of this slot. Check the ESP app for your full day's schedule."
            ),
            data_source="live",
        )

    # ── 2. Check municipal fault / maintenance data ───────────────────────
    # Always runs regardless of ESP status.
    muni = municipal_provider.get_municipal_outage(municipality, suburb)
    if muni:
        return _result(
            status=muni["status"],
            source=muni["source"],
            started_at=muni["started_at"],
            estimated_restore=muni["estimated_restore"],
            planned=muni["status"] == "Planned Maintenance",
            message=muni["message"],
            data_source="live",
        )

    # ── 3. Nothing found — build an honest "no issues" response ──────────
    # The message and dataSource reflect what we actually checked
    # (or couldn't check) so the customer is never misled.

    if esp_issue == RT_RATE_LIMITED:
        # Municipal checked OK (nothing found), but loadshedding unknown
        source = _municipal_source(municipality)
        return _result(
            status="No Reported Issues",
            source=source,
            started_at=None,
            estimated_restore=None,
            planned=False,
            message=(
                "No active electricity faults were detected for your area. "
                "Note: loadshedding status could not be checked — the daily "
                "ESP API request limit has been reached (50 requests/day). "
                "To check loadshedding manually, visit loadshedding.eskom.co.za."
            ),
            data_source="partial",
        )

    if esp_issue in (RT_ERROR, RT_NOT_CONFIGURED):
        source = _municipal_source(municipality)
        reason = (
            "API token not configured" if esp_issue == RT_NOT_CONFIGURED
            else esp.get("message", "ESP API error")
        )
        return _result(
            status="No Reported Issues",
            source=source,
            started_at=None,
            estimated_restore=None,
            planned=False,
            message=(
                "No active electricity faults were detected for your area. "
                f"Note: loadshedding status is unavailable ({reason}). "
                "If you are still experiencing connectivity issues, this may be "
                "due to a device, router, or internal network problem — try "
                "restarting your router. If the problem persists, contact "
                "ConnectCo support."
            ),
            data_source="partial",
        )

    # ESP checked OK and municipal checked — nothing found anywhere
    if municipal_provider.is_integrated(municipality):
        source  = f"EskomSePush (ESP) + {municipality}"
        message = (
            "No loadshedding or active electricity faults have been detected "
            "in your area. If you are still experiencing connectivity issues, "
            "this may be due to a device, router, or internal network problem — "
            "try restarting your router and checking your devices. "
            "If the problem persists, please contact ConnectCo support."
        )
    else:
        source  = f"EskomSePush (ESP) — {esp['region_label']}"
        message = (
            "No loadshedding has been detected in your area. "
            f"Live municipal fault data is not yet available for {municipality}. "
            "If you are still experiencing connectivity issues, this may be due "
            "to a device, router, or internal network problem — try restarting "
            "your router. If the problem persists, please contact ConnectCo support."
        )

    return _result(
        status="No Reported Issues",
        source=source,
        started_at=None,
        estimated_restore=None,
        planned=False,
        message=message,
        data_source="live",
    )


# ── Response builders ─────────────────────────────────────────────────────────

def _result(
    status: str,
    source: str,
    started_at: Optional[datetime],
    estimated_restore: Optional[datetime],
    planned: bool,
    message: str,
    data_source: str,
) -> Dict[str, Any]:
    return {
        "status":           status,
        "source":           source,
        "startedAt":        started_at.isoformat() if started_at else None,
        "estimatedRestore": estimated_restore.isoformat() if estimated_restore else None,
        "planned":          planned,
        "message":          message,
        "dataSource":       data_source,
    }


def _unavailable(source: str, reason: str, message: str) -> Dict[str, Any]:
    return {
        "status":           "Data Unavailable",
        "source":           source,
        "reason":           reason,
        "startedAt":        None,
        "estimatedRestore": None,
        "planned":          False,
        "message":          message,
        "dataSource":       "unavailable",
    }


def _municipal_source(municipality: str) -> str:
    """Returns the source label reflecting which municipal data was checked."""
    if municipal_provider.is_integrated(municipality):
        return municipality
    return "Municipal data not yet integrated for this area"
