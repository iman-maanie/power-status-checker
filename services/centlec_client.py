"""
centlec_client.py
------------------
Live integration for Centlec electricity interruption notices.

Centlec (Central Electric) is the electricity distributor for:
    - Mangaung Metropolitan Municipality (Bloemfontein, Botshabelo, Thaba Nchu)
    - Several surrounding Free State towns (Wepener, Dewetsdorp, Vanstadensrus, etc.)

DATA SOURCE
-----------
HTML table scraped from:
    https://centlec.co.za/Media/PowerInterruptionDocuments

Each row in the table contains:
    - Name:           Comma-separated list of affected suburbs/areas
    - Published Date: Date the notice was published (YYYY-MM-DD)
    - Action:         Link to a PDF with full details (start time, end time, reason)

IMPORTANT LIMITATION — HISTORICAL RECORDS
------------------------------------------
Centlec does NOT remove entries from the table once an interruption is resolved.
The table contains records going back several months. To avoid reporting resolved
outages, we filter to entries published within the last CENTLEC_MAX_AGE_DAYS days
(default: 14). This is a heuristic — most interruptions in this area are resolved
within 1–2 weeks, but complex infrastructure repairs can take longer.

WHY NOT PDF PARSING?
---------------------
Each row links to a PDF containing exact start/end times. We intentionally skip
PDF parsing in this version for two reasons:
  1. PDF text layout varies per document and is fragile to parse reliably.
  2. The HTML table alone is sufficient for suburb matching and approximate dates.

PHASE 2 — PDF PARSING (future enhancement)
-------------------------------------------
When exact start/end times are needed, download the PDF for matched entries and
parse the text using `pdfplumber`:
    pip install pdfplumber
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = pdf.pages[0].extract_text()
    # Then regex-match for patterns like "Start Time: 08:00" or "End Time: 16:00"
A helper function stub is provided at the bottom of this file.

SUBURB MATCHING
---------------
The Name column contains entries like:
    "Bayswater, Dan Pienaar, Noordhoek, Part of Waverly, Part of Heliconhoogte etc"
We split on commas, strip "Part of" / "Parts of" prefixes and trailing "etc",
then do case-insensitive containment matching against the customer's suburb.

CACHING
-------
The page is cached for CENTLEC_CACHE_TTL_SECONDS (default: 900 = 15 minutes).
"""

import io
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("connectco.centlec_client")

_URL        = "https://centlec.co.za/Media/PowerInterruptionDocuments"
_BASE_URL   = "https://centlec.co.za"
_TIMEOUT    = 12

# Only match entries published within this many days (heuristic for "still active")
_MAX_AGE_DAYS: int = int(os.environ.get("CENTLEC_MAX_AGE_DAYS", "14"))

# Cache the full parsed entry list to avoid re-scraping on every request
_cache_data: Optional[List[Dict]] = None
_cache_ts: float = 0.0
_CACHE_TTL: int = int(os.environ.get("CENTLEC_CACHE_TTL_SECONDS", "900"))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_electricity_outage(suburb: str) -> Optional[Dict[str, Any]]:
    """
    Returns the most recent active electricity interruption notice for the
    given suburb, or None if nothing is found or the feed is unavailable.

    Return shape (on match):
        {
            "status":            "Power Outage" | "Planned Maintenance",
            "source":            str,
            "started_at":        datetime,     # midnight on published date (approx)
            "estimated_restore": None,          # not available from HTML; needs PDF
            "message":           str,
        }
    """
    entries = _fetch_and_parse()
    if entries is None:
        return None

    cutoff  = date.today() - timedelta(days=_MAX_AGE_DAYS)
    suburb_norm = suburb.upper().strip()

    # Entries are already in newest-first order (as listed on the page)
    for entry in entries:
        if entry["published_date"] < cutoff:
            break  # entries are sorted newest-first; no point continuing
        if _suburb_matches(entry["name"], suburb_norm):
            logger.info(
                "Centlec match for suburb '%s': published %s — %s",
                suburb, entry["published_date"], entry["name"][:80],
            )
            # Attempt to extract exact times from the linked PDF.
            # Falls back to None/None gracefully if OCR isn't available
            # or the PDF download fails.
            pdf_url = entry.get("pdf_url") or ""
            start_dt, end_dt = (
                _extract_times_from_pdf(pdf_url) if pdf_url else (None, None)
            )
            return _format_entry(entry, start_dt, end_dt)

    return None


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def _fetch_and_parse() -> Optional[List[Dict]]:
    """
    Returns the list of interruption entries parsed from the HTML table,
    using the in-memory TTL cache. Returns None on failure.
    """
    global _cache_data, _cache_ts

    age = time.time() - _cache_ts
    if _cache_data is not None and age < _CACHE_TTL:
        logger.debug("Centlec cache hit (age %.0fs / TTL %ds)", age, _CACHE_TTL)
        return _cache_data

    try:
        r = requests.get(_URL, timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Centlec page fetch failed: %s", exc)
        return None

    entries = _parse_table(r.text)
    if entries is not None:
        _cache_data = entries
        _cache_ts   = time.time()
        logger.debug("Centlec parsed %d entries from table", len(entries))

    return entries


def _parse_table(html: str) -> Optional[List[Dict]]:
    """
    Parse the power interruption HTML table into a list of entry dicts.
    Returns None if the table cannot be found or parsed.

    Expected table columns:
        [0] #  |  [1] Name  |  [2] Published Date  |  [3] Category  |  [4] Action
    """
    try:
        soup  = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            logger.warning("Centlec: could not find a <table> on the page.")
            return None

        entries = []
        rows    = table.find_all("tr")

        for row in rows[1:]:   # skip header row
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            name_raw       = cells[1].get_text(separator=" ", strip=True)
            date_raw       = cells[2].get_text(strip=True)
            is_planned     = "planned" in name_raw.lower()

            # PDF URL from the View link in the last cell
            pdf_url: Optional[str] = None
            action_cell = cells[-1]
            link = action_cell.find("a")
            if link and link.get("href"):
                href = link["href"]
                pdf_url = href if href.startswith("http") else _BASE_URL + href

            try:
                published = datetime.strptime(date_raw, "%Y-%m-%d").date()
            except ValueError:
                logger.debug("Centlec: could not parse date '%s' — skipping row.", date_raw)
                continue

            entries.append({
                "name":           name_raw,
                "published_date": published,
                "is_planned":     is_planned,
                "pdf_url":        pdf_url,
            })

        return entries

    except Exception as exc:
        logger.warning("Centlec: HTML parsing error — %s: %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# Suburb matching
# ---------------------------------------------------------------------------

def _suburb_matches(name: str, suburb_norm: str) -> bool:
    """
    Split the Name column on commas, normalise each token, and check if the
    customer's suburb matches any of them.

    Handles entries like:
        "Bayswater, Dan Pienaar, Noordhoek, Part of Waverly, etc"
    """
    for token in name.split(","):
        # Strip "Part of", "Parts of", "etc", leading/trailing whitespace
        cleaned = re.sub(r"(?i)\bparts?\s+of\s+", "", token)
        cleaned = re.sub(r"(?i)\betc\.?\b",       "", cleaned).strip().upper()
        if not cleaned:
            continue
        if suburb_norm in cleaned or cleaned in suburb_norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _format_entry(entry: Dict, start_dt=None, end_dt=None) -> Dict[str, Any]:
    status     = "Planned Maintenance" if entry["is_planned"] else "Power Outage"
    name_clean = entry["name"].strip()
    pdf_url    = entry.get("pdf_url") or ""

    # Use exact start time from PDF if available;
    # fall back to midnight on the published date as an approximation
    if start_dt is None:
        from datetime import timezone, timedelta
        sast      = timezone(timedelta(hours=2))
        start_dt  = datetime.combine(entry["published_date"], datetime.min.time()).replace(tzinfo=sast)
        time_note = (
            f" Published date: {entry['published_date'].strftime('%d %B %Y')}. "
            f"Exact start time unavailable."
        )
    else:
        time_note = ""

    pdf_note = f" Full details: {pdf_url}" if pdf_url else ""

    return {
        "status":            status,
        "source":            "Centlec (centlec.co.za) — live",
        "started_at":        start_dt,
        "estimated_restore": end_dt,
        "message": (
            f"A{' planned' if entry['is_planned'] else 'n'} electricity interruption "
            f"has been reported by Centlec for your area.{time_note} "
            f"Affected areas: {name_clean}.{pdf_note}"
        ),
    }


# ---------------------------------------------------------------------------
# PDF parsing — extracts exact start/end times via OCR
# ---------------------------------------------------------------------------

# Requires: pip install pymupdf pytesseract
# Tesseract OCR must also be installed on the server:
#   Ubuntu/Debian: sudo apt-get install tesseract-ocr
#   macOS:        brew install tesseract
#   Windows:      https://github.com/UB-Mannheim/tesseract/wiki

_MONTH_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def _extract_times_from_pdf(pdf_url: str):
    """
    Downloads the PDF for a Centlec interruption notice and uses OCR to
    extract the start and end datetimes.

    Returns (start_dt, end_dt) as tz-aware SAST datetimes, or (None, None)
    if extraction fails for any reason.

    The Centlec PDF format is consistent across documents:
        "...will be interrupted from 07H00 until 18H00 on the 05th of July 2026..."
    We normalise OCR artefacts (O vs 0 confusion after the H) before matching.
    """
    try:
        import fitz           # PyMuPDF — renders PDF pages to images
        import pytesseract    # OCR wrapper for Tesseract
        from PIL import Image
    except ImportError:
        logger.debug(
            "Centlec PDF parsing skipped — pymupdf/pytesseract not installed. "
            "Run: pip install pymupdf pytesseract"
        )
        return None, None

    try:
        r = requests.get(pdf_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        pdf_bytes = r.content
    except requests.RequestException as exc:
        logger.warning("Centlec PDF download failed (%s): %s", pdf_url, exc)
        return None, None

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        full_text = ""
        for page in doc:
            # 2× zoom gives Tesseract enough resolution for reliable OCR
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            full_text += pytesseract.image_to_string(img) + " "

        # Centlec PDFs are image-based (not text-searchable), so OCR is the
        # only extraction path. The main artefact is O/0 confusion in times:
        # "07H00" is often read as "07HO0" or "07HO00". We normalise by
        # replacing any [Oo0]{1,3} sequence immediately after H/h with "00".
        normalised = re.sub(
            r"(\d{1,2}[Hh])[Oo0]{1,3}",
            lambda m: m.group(1) + "00",
            full_text,
        )

        # Primary pattern: "from 07H00 until 18H00 on the 05th of July 2026"
        # [^\s]* matches ordinal suffixes (th/st/nd/rd) even when OCR reads
        # them as punctuation (e.g. " or ').
        pat = (
            r"from\s+(\d{1,2})[Hh](\d{2})\s+"
            r"until\s+(\d{1,2})[Hh](\d{2})\s+"
            r"on\s+the\s+(\d+)[^\s]*\s+of\s+(\w+)\s+(\d{4})"
        )
        m = re.search(pat, normalised, re.IGNORECASE)
        if not m:
            logger.debug("Centlec PDF: time pattern not found. URL: %s", pdf_url)
            return None, None

        start_h, start_m, end_h, end_m, day, month_str, year = m.groups()
        month = _MONTH_MAP.get(month_str.lower())
        if not month:
            logger.debug("Centlec PDF: unknown month '%s'", month_str)
            return None, None

        from datetime import timezone, timedelta
        sast     = timezone(timedelta(hours=2))
        start_dt = datetime(int(year), month, int(day), int(start_h), int(start_m), tzinfo=sast)
        end_dt   = datetime(int(year), month, int(day), int(end_h),   int(end_m),   tzinfo=sast)

        logger.info("Centlec PDF parsed OK: start=%s end=%s", start_dt, end_dt)
        return start_dt, end_dt

    except Exception as exc:
        logger.warning("Centlec PDF parsing error for %s: %s", pdf_url, exc)
        return None, None
