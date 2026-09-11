/* =========================================================================
   Power Status Checker — script.js (ConnectCo)

   Responsibilities:
     1. Populate cascading Province → Municipality → Suburb dropdowns.
     2. Call GET /api/power-status and display the result.
     3. Handle all status states including "Data Unavailable" (rate limit,
        API error, not configured) with clear user-facing messaging.
   ========================================================================= */

(function () {
    "use strict";

    // -----------------------------------------------------------------------
    // Location data
    // Note: In production this would come from a geo/municipal API endpoint
    // rather than being hardcoded here. The shape (province → municipality →
    // suburbs array) is the contract — swapping the source doesn't change
    // anything else in this file.
    // -----------------------------------------------------------------------
    const LOCATION_DATA = {
        "Western Cape": {
            "City of Cape Town": [
                // Atlantic Seaboard & City Bowl
                "Sea Point", "Green Point", "De Waterkant", "Bo-Kaap", "Gardens",
                "Oranjezicht", "Tamboerskloof", "Vredehoek", "Observatory",
                "Woodstock", "Salt River", "Cape Town CBD",
                // Southern Suburbs
                "Rondebosch", "Newlands", "Claremont", "Kenilworth", "Wynberg",
                "Plumstead", "Diep River", "Bergvliet", "Constantia", "Tokai",
                "Meadowridge", "Lansdowne", "Pinelands",
                // False Bay & South Peninsula
                "Muizenberg", "Kalk Bay", "Fish Hoek", "Glencairn", "Simon's Town",
                "Noordhoek", "Ocean View", "Kommetjie", "Lavender Hill",
                // Northern Suburbs
                "Milnerton", "Table View", "Bloubergstrand", "Parklands",
                "Bellville", "Goodwood", "Parow", "Brackenfell", "Kraaifontein",
                // Cape Flats & Mitchells Plain
                "Mitchells Plain", "Khayelitsha", "Hanover Park", "Bonteheuwel",
                "Tafelsig", "Langa", "Guguletu", "Nyanga", "Mfuleni",
                // Southern Cape Flats
                "Grassy Park", "Lotus River", "Philippi",
                // West Coast corridor
                "Hout Bay", "Llandudno",
                // East / Helderberg
                "Somerset West", "Strand", "Gordon's Bay",
                // Far South
                "Stellenridge", "Pinati Estate"
            ],
            "Drakenstein Local Municipality": ["Paarl", "Wellington", "Franschhoek"],
            "Stellenbosch Local Municipality": ["Stellenbosch Central", "Cloetesville", "Jamestown"],
        },
        "Gauteng": {
            "City of Johannesburg": [
                "Braamfontein", "Fourways", "Johannesburg CBD", "Melville",
                "Midrand", "Randburg", "Rosebank", "Sandton", "Soweto"
            ],
            "City of Tshwane": [
                "Arcadia", "Centurion", "Hatfield", "Mamelodi",
                "Pretoria CBD", "Sunnyside", "Waterkloof"
            ],
            "Ekurhuleni Metropolitan Municipality": [
                "Benoni", "Boksburg", "Brakpan", "Germiston",
                "Kempton Park", "Springs"
            ],
        },
        "KwaZulu-Natal": {
            "eThekwini Metropolitan Municipality": [
                // Central Durban
                "Durban CBD", "Berea", "Musgrave", "Morningside", "Overport",
                "Greyville", "Glenwood", "Umbilo", "Congella",
                // Northern suburbs
                "Umhlanga", "La Lucia", "Durban North", "Redhill", "Phoenix",
                "Mount Edgecombe", "Tongaat", "Ballito",
                // Western suburbs
                "Pinetown", "Westville", "Hillcrest", "Kloof", "Gillitts",
                "New Germany", "Marianhill", "Cato Ridge",
                // Southern suburbs
                "Chatsworth", "Umlazi", "Isipingo", "Amanzimtoti",
                "Warner Beach", "Bluff", "Wentworth",
                // Inland/outlying
                "Inanda", "Ntuzuma", "KwaMashu", "Verulam", "Stanger"
            ],
            "Msunduzi Local Municipality": ["Pietermaritzburg Central", "Northdale", "Edendale"],
        },
        "Eastern Cape": {
            "Nelson Mandela Bay Metropolitan Municipality": [
                "Gqeberha CBD", "Newton Park", "Summerstrand", "Walmer"
            ],
            "Buffalo City Metropolitan Municipality": [
                "Beacon Bay", "Cambridge", "East London CBD", "Vincent"
            ],
        },
        "Free State": {
            "Mangaung Metropolitan Municipality": [
                // Bloemfontein suburbs
                "Bayswater", "Dan Pienaar", "Noordhoek", "Waverly", "Heliconhoogte",
                "Ribblesdale", "Langenhoven Park", "Bloemdal", "Ferreira", "Wilgehof",
                "Hospital Park", "Fichardt Park", "Navalsig", "Hilton", "Naval Park",
                "Estoire", "Olive Hill", "Groenvlei", "Mooiwater", "Sophieshoogte",
                "Bainsvlei", "Waterbron", "Rayton", "Roodewal", "Heidedal",
                "Bloemside", "Freedom Square", "Bloemfontein CBD", "Brandwag",
                "Universitas", "Lakeview", "Bergman Square", "Gardenia Park",
                "Chester Hill", "Spitskop", "Tibbie Visser",
                // Botshabelo (also Mangaung Metro)
                "Botshabelo",
                // Thaba Nchu
                "Thaba Nchu",
                // Smaller Free State towns served by Centlec
                "Wepener", "Dewetsdorp", "Vanstadensrus",
            ],
        },
        "Limpopo": {
            "Polokwane Local Municipality": ["Polokwane CBD", "Bendor", "Flora Park", "Seshego"],
        },
        "Mpumalanga": {
            "Mbombela Local Municipality": ["Nelspruit CBD", "Riverside Park", "White River"],
        },
        "North West": {
            "Rustenburg Local Municipality": ["Rustenburg CBD", "Boitekong", "Tlhabane"],
        },
        "Northern Cape": {
            "Sol Plaatje Local Municipality": ["Kimberley CBD", "Galeshewe", "Roodepan"],
        },
    };

    // -----------------------------------------------------------------------
    // Status → visual style mapping
    // -----------------------------------------------------------------------
    const STATUS_STYLES = {
        "No Reported Issues":  { modifier: "green",  icon: iconCheck()       },
        "Planned Maintenance": { modifier: "orange", icon: iconWrench()      },
        "Loadshedding":        { modifier: "blue",   icon: iconBolt()        },
        "Power Outage":        { modifier: "red",    icon: iconWarning()     },
        "Data Unavailable":    { modifier: "grey",   icon: iconUnavailable() },
    };

    // Friendly labels for each "unavailable" reason code returned by the API
    const REASON_LABELS = {
        "rate_limited":    "Daily API limit reached",
        "api_error":       "API connection error",
        "not_configured":  "Service not configured",
    };

    // -----------------------------------------------------------------------
    // DOM refs
    // -----------------------------------------------------------------------
    const provinceEl     = document.getElementById("province");
    const municipalityEl = document.getElementById("municipality");
    const suburbEl       = document.getElementById("suburb");
    const form           = document.getElementById("power-status-form");
    const checkBtn       = document.getElementById("check-btn");
    const btnSpinner     = document.getElementById("btn-spinner");
    const formError      = document.getElementById("form-error");

    const emptyCard         = document.getElementById("empty-card");
    const resultCard        = document.getElementById("result-card");
    const resultStatusEl    = document.getElementById("result-status");
    const resultIconEl      = document.getElementById("result-icon");
    const resultStatusText  = document.getElementById("result-status-text");
    const resultSourceEl    = document.getElementById("result-source");
    const resultStartedEl   = document.getElementById("result-started");
    const resultRestoreEl   = document.getElementById("result-restore");
    const resultMessageEl   = document.getElementById("result-message");
    const dataSourceBadge   = document.getElementById("data-source-badge");
    const reasonBannerEl    = document.getElementById("reason-banner");

    // -----------------------------------------------------------------------
    // Dropdown population
    // -----------------------------------------------------------------------

    function populateProvinces() {
        Object.keys(LOCATION_DATA).sort().forEach((province) => {
            provinceEl.appendChild(makeOption(province, province));
        });
    }

    function resetSelect(el, placeholder) {
        el.innerHTML = "";
        el.appendChild(makeOption("", placeholder, true, true));
    }

    function makeOption(value, label, disabled = false, selected = false) {
        const opt = document.createElement("option");
        opt.value    = value;
        opt.textContent = label;
        opt.disabled = disabled;
        opt.selected = selected;
        return opt;
    }

    provinceEl.addEventListener("change", () => {
        const province = provinceEl.value;
        const munis    = Object.keys(LOCATION_DATA[province] || {}).sort();

        resetSelect(municipalityEl, "Select a municipality");
        munis.forEach(m => municipalityEl.appendChild(makeOption(m, m)));
        municipalityEl.disabled = munis.length === 0;

        resetSelect(suburbEl, "Select a municipality first");
        suburbEl.disabled = true;

        hideError();
    });

    municipalityEl.addEventListener("change", () => {
        const province     = provinceEl.value;
        const municipality = municipalityEl.value;
        const suburbs      = (LOCATION_DATA[province]?.[municipality] || []).slice().sort();

        resetSelect(suburbEl, "Select a suburb");
        suburbs.forEach(s => suburbEl.appendChild(makeOption(s, s)));
        suburbEl.disabled = suburbs.length === 0;

        hideError();
    });

    suburbEl.addEventListener("change", hideError);

    // -----------------------------------------------------------------------
    // Form submit → API call
    // -----------------------------------------------------------------------

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        hideError();

        const province     = provinceEl.value;
        const municipality = municipalityEl.value;
        const suburb       = suburbEl.value;

        if (!province || !municipality || !suburb) {
            showError("Please select a province, municipality, and suburb before checking.");
            return;
        }

        setLoading(true);

        try {
            const params   = new URLSearchParams({ province, municipality, suburb });
            const response = await fetch(`/api/power-status?${params}`, {
                method:  "GET",
                headers: { Accept: "application/json" },
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data?.error || "Unable to check power status right now.");
            }

            renderResult(data);
        } catch (err) {
            showError(err.message || "Something went wrong. Please try again.");
        } finally {
            setLoading(false);
        }
    });

    // -----------------------------------------------------------------------
    // Result rendering
    // -----------------------------------------------------------------------

    function renderResult(data) {
        const style = STATUS_STYLES[data.status] || STATUS_STYLES["Data Unavailable"];

        // Status pill
        resultStatusEl.className = `result-status result-status--${style.modifier}`;
        resultIconEl.innerHTML   = style.icon;
        resultStatusText.textContent = data.status;

        // Detail rows
        resultSourceEl.textContent  = data.source || "—";
        resultStartedEl.textContent = formatTimestamp(data.startedAt);
        resultRestoreEl.textContent = formatTimestamp(data.estimatedRestore);
        resultMessageEl.textContent = data.message || "";

        // Data-source badge (top-right of card)
        if (data.dataSource === "live") {
            dataSourceBadge.hidden    = false;
            dataSourceBadge.className = "data-source-badge data-source-badge--live";
            dataSourceBadge.textContent = "● Live Data";
        } else if (data.dataSource === "partial") {
            dataSourceBadge.hidden    = false;
            dataSourceBadge.className = "data-source-badge data-source-badge--partial";
            dataSourceBadge.textContent = "⚠ Partial Data";
        } else if (data.dataSource === "unavailable") {
            dataSourceBadge.hidden    = false;
            dataSourceBadge.className = "data-source-badge data-source-badge--unavailable";
            dataSourceBadge.textContent = "● Unavailable";
        } else {
            dataSourceBadge.hidden = true;
        }

        // Reason banner — only shown for "Data Unavailable" to give the
        // customer a clear, specific explanation beyond just the message text.
        if (data.status === "Data Unavailable" && data.reason) {
            reasonBannerEl.hidden = false;
            const label = REASON_LABELS[data.reason] || data.reason;
            reasonBannerEl.querySelector(".reason-banner__label").textContent = label;
            reasonBannerEl.querySelector(".reason-banner__detail").textContent =
                data.reason === "rate_limited"
                    ? "The loadshedding API allows 50 requests per day on the current plan. This limit has been reached and will reset at midnight."
                    : "The loadshedding data service could not be reached. This is usually temporary.";
        } else {
            reasonBannerEl.hidden = true;
        }

        emptyCard.hidden = true;
        resultCard.hidden = false;
        // Re-trigger fade-in animation
        resultCard.classList.remove("result-card");
        void resultCard.offsetWidth;
        resultCard.classList.add("result-card");
    }

    // -----------------------------------------------------------------------
    // Timestamp formatting
    // -----------------------------------------------------------------------

    /**
     * Converts an ISO-8601 timestamp to a human-friendly local time + relative
     * description, e.g. "14:05 · started 1h 40m ago" or "18:30 · in 2h 5m".
     */
    function formatTimestamp(iso) {
        if (!iso) return "Not applicable";

        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return "Not applicable";

        const time = date.toLocaleTimeString(undefined, {
            hour: "2-digit", minute: "2-digit",
        });

        const diffMs      = date.getTime() - Date.now();
        const totalMins   = Math.round(Math.abs(diffMs) / 60_000);
        const hours       = Math.floor(totalMins / 60);
        const mins        = totalMins % 60;
        const parts       = [];
        if (hours > 0) parts.push(`${hours}h`);
        if (mins  > 0) parts.push(`${mins}m`);

        let relative;
        if (parts.length === 0) {
            relative = "just now";
        } else if (diffMs < 0) {
            relative = `${parts.join(" ")} ago`;
        } else {
            relative = `in ${parts.join(" ")}`;
        }

        return `${time} · ${relative}`;
    }

    // -----------------------------------------------------------------------
    // UI helpers
    // -----------------------------------------------------------------------

    function setLoading(loading) {
        checkBtn.disabled = loading;
        btnSpinner.hidden = !loading;
        checkBtn.querySelector(".btn-check__label").textContent =
            loading ? "Checking…" : "Check Power Status";
    }

    function showError(msg) {
        formError.textContent = msg;
        formError.hidden = false;
    }

    function hideError() {
        formError.hidden    = true;
        formError.textContent = "";
    }

    // -----------------------------------------------------------------------
    // SVG icons
    // -----------------------------------------------------------------------

    function iconCheck() {
        return `<svg viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.15"/>
            <path d="M7 12.5l3 3 7-7" stroke="currentColor" stroke-width="2"
                  stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`;
    }

    function iconWrench() {
        return `<svg viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.15"/>
            <path d="M14.7 6.3a3.5 3.5 0 00-4.6 4.2L6 14.6V18h3.4l4.1-4.1a3.5
                     3.5 0 004.2-4.6l-2.3 2.3-1.7-1.7 2.3-2.3z"
                  stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        </svg>`;
    }

    function iconBolt() {
        return `<svg viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.15"/>
            <path d="M13 3L5 14h5l-1 7 8-11h-5l1-7z" fill="currentColor"/>
        </svg>`;
    }

    function iconWarning() {
        return `<svg viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.15"/>
            <path d="M12 8v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <circle cx="12" cy="16" r="1" fill="currentColor"/>
        </svg>`;
    }

    function iconUnavailable() {
        return `<svg viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.15"/>
            <path d="M8 8l8 8M16 8l-8 8" stroke="currentColor" stroke-width="2"
                  stroke-linecap="round"/>
        </svg>`;
    }

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------
    populateProvinces();

})();
