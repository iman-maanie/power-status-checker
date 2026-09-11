"""
app.py
------
Flask entry point for the ConnectCo Power Status Checker.

Routes:
    GET /                   → customer dashboard
    GET /api/power-status   → live power status JSON
"""

from dotenv import load_dotenv
load_dotenv()  # must be first — loads .env before any service module reads os.environ

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)

from flask import Flask, render_template, request, jsonify
from services.power_service import get_power_status
from services import eskom_client

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/power-status", methods=["GET"])
def api_power_status():
    province     = request.args.get("province",     "").strip()
    municipality = request.args.get("municipality", "").strip()
    suburb       = request.args.get("suburb",       "").strip()

    if not province or not municipality or not suburb:
        return jsonify({"error": "province, municipality, and suburb are all required."}), 400

    try:
        result = get_power_status(province, municipality, suburb)
        return jsonify(result)
    except Exception as exc:
        app.logger.exception("Unhandled error in get_power_status for %s/%s/%s: %s",
                             province, municipality, suburb, exc)
        return jsonify({
            "status":           "Data Unavailable",
            "source":           "Internal",
            "reason":           "api_error",
            "startedAt":        None,
            "estimatedRestore": None,
            "planned":          False,
            "message":          "An unexpected error occurred while checking power status. Please try again.",
            "dataSource":       "unavailable",
        }), 500


@app.errorhandler(Exception)
def handle_exception(exc):
    """
    Catch-all handler — ensures the API never returns HTML to the frontend,
    even on completely unhandled exceptions. Returns a JSON error response.
    """
    app.logger.exception("Unhandled exception: %s", exc)
    return jsonify({
        "status":           "Data Unavailable",
        "source":           "Internal",
        "reason":           "api_error",
        "startedAt":        None,
        "estimatedRestore": None,
        "planned":          False,
        "message":          "An unexpected server error occurred. Please try again shortly.",
        "dataSource":       "unavailable",
    }), 500


@app.route("/api/esp-quota", methods=["GET"])
def api_esp_quota():
    """
    GET /api/esp-quota
    Returns current ESP API token usage. Useful for checking how many
    of your 50 daily requests have been used.
    """
    allowance = eskom_client.get_allowance()
    if allowance is None:
        return jsonify({"error": "Could not retrieve ESP token usage."}), 503
    remaining = allowance.get("limit", 50) - allowance.get("count", 0)
    return jsonify({
        "used":      allowance.get("count"),
        "limit":     allowance.get("limit"),
        "remaining": remaining,
        "type":      allowance.get("type"),
    })


if __name__ == "__main__":
    print()
    if eskom_client.is_configured():
        print("  ✅  ESP_API_TOKEN detected — Eskom loadshedding data is LIVE.")
        print(f"      Response caching: {eskom_client._CACHE_TTL // 60} min "
              f"(free tier: 50 req/day — set ESP_CACHE_TTL_SECONDS to adjust).")
        allowance = eskom_client.get_allowance()
        if allowance:
            used      = allowance.get("count", "?")
            limit     = allowance.get("limit", 50)
            remaining = limit - used if isinstance(used, int) else "?"
            bar       = "█" * int(used / limit * 20) + "░" * (20 - int(used / limit * 20)) if isinstance(used, int) else ""
            print(f"      Token usage today: {used}/{limit} used, {remaining} remaining  [{bar}]")
        else:
            print("      (Could not fetch token usage — check your token is valid.)")
    else:
        print("  ⚠️   ESP_API_TOKEN not set — add it to your .env file.")
        print("       Get a free token at: https://eskomsepush.gumroad.com/l/api")
    print("  ✅  City of Cape Town — electricity faults & planned maintenance (live).")
    print("  ✅  eThekwini (Durban)  — medium voltage electricity faults (live).")
    print("  ✅  Mangaung (Bloemfontein) — Centlec interruption notices (live, last 14 days).")
    print("  ⏳  City of Johannesburg, Tshwane, Nelson Mandela Bay — pending integration.")
    print()
    app.run(debug=True, host="0.0.0.0", port=5000)
