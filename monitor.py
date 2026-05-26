#!/usr/bin/env python3
"""
Monitor Hampton Inn Lexington Historic District (LXTSWHX) for room availability.

Default mode (production):
  Checks May 22–30, 2027 with 2/3/4 night stays (2 rooms, 2 adults each)
  and sends an ntfy.sh push notification when any rooms become bookable.

Test mode (set via env vars):
  TEST_ARRIVAL  – override start date (YYYY-MM-DD)
  TEST_NIGHTS   – override stay lengths, comma-separated (e.g. "2" or "2,3,4")
  TEST_DAYS     – number of check-in days to scan starting from TEST_ARRIVAL (default 1)
  SKIP_NOTIFY=1 – classify and log but do not send ntfy notification
  DEBUG=1       – dump rendered HTML + screenshot per check to ./debug/ for inspection

Failure-state semantics:
  DATES_NOT_OPEN – booking window hasn't opened yet  → no notification
  NO_ROOMS       – dates open but no inventory       → no notification
  ONE_ROOM/TWO_ROOMS – at least one room bookable    → notification sent
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Configuration ─────────────────────────────────────────────────────────────

CTYHOCN         = "LXTSWHX"
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC", "")
SKIP_NOTIFY     = os.environ.get("SKIP_NOTIFY", "").lower() in ("1", "true", "yes")
DEBUG           = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
ADULTS_PER_ROOM = 2
REQUEST_DELAY   = 1.5

# Date range can be overridden for test runs via env vars
_TEST_ARRIVAL = os.environ.get("TEST_ARRIVAL", "").strip()
_TEST_NIGHTS  = os.environ.get("TEST_NIGHTS", "").strip()
_TEST_DAYS    = os.environ.get("TEST_DAYS", "").strip()

if _TEST_ARRIVAL:
    CHECK_IN_START = datetime.strptime(_TEST_ARRIVAL, "%Y-%m-%d").date()
    CHECK_IN_DAYS  = int(_TEST_DAYS) if _TEST_DAYS else 1
    STAY_LENGTHS   = (
        [int(n.strip()) for n in _TEST_NIGHTS.split(",")] if _TEST_NIGHTS else [2]
    )
    MODE = "TEST"
else:
    CHECK_IN_START = date(2027, 5, 22)
    CHECK_IN_DAYS  = 9
    STAY_LENGTHS   = [2, 3, 4]
    MODE = "PROD"

DEBUG_DIR = Path("debug")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Availability states ───────────────────────────────────────────────────────

class Avail(Enum):
    DATES_NOT_OPEN = auto()
    NO_ROOMS       = auto()
    ONE_ROOM       = auto()
    TWO_ROOMS      = auto()
    UNKNOWN        = auto()


@dataclass
class CheckResult:
    arrival:   date
    departure: date
    avail:     Avail
    notes:     str = field(default="")


# ── URL helpers ───────────────────────────────────────────────────────────────

def booking_url(arrival: date, departure: date, num_rooms: int = 2) -> str:
    room_params = "&".join(
        f"room{i}NumAdults={ADULTS_PER_ROOM}" for i in range(1, num_rooms + 1)
    )
    return (
        f"https://www.hilton.com/en/book/reservation/rooms/"
        f"?ctyhocn={CTYHOCN}"
        f"&arrivalDate={arrival.isoformat()}"
        f"&departureDate={departure.isoformat()}"
        f"&{room_params}"
    )


# ── HTTP session ──────────────────────────────────────────────────────────────

_SESSION: Optional[requests.Session] = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3, backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503],
                )
            ),
        )
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
        })
        _SESSION = s
    return _SESSION


# ── Classifier signals ────────────────────────────────────────────────────────

_NOT_OPEN = [
    "not available for booking",
    "dates are not available",
    "not yet available",
    "outside the booking window",
    "too far in advance",
    "maximum advance",
    "advance purchase period",
    "cannot be booked",
    "no longer accepting",
    "we are unable to find a hotel",
]
# "no rooms" signals — only meaningful when "rooms found" is ABSENT, because
# Hilton's pages display partial sold-out copy like "10 rooms found. 3 are
# currently sold out." even when inventory exists.
_NO_ROOMS = [
    "sorry, we don't have any rooms available",
    "we don't have any rooms available for those dates",
    "no rooms available for the selected dates",
    "no rooms match your criteria",
    "we are unable to find rates",
    "no hotel rooms were found",
    "we couldn't find any rooms",
    "0 rooms available",
]

_ROOMS_FOUND  = re.compile(r"\b(\d+)\s+rooms?\s+found\b", re.IGNORECASE)
_PRICE_PATTERN = re.compile(r"\$\s?\d{2,4}\b|\d{2,4}\s?usd", re.IGNORECASE)
_PER_NIGHT     = re.compile(r"\bper\s+night\b|/\s*night\b|avg/?\s*night", re.IGNORECASE)
_WAF_MARKER    = re.compile(r"hilton page reference code|something went wrong",
                            re.IGNORECASE)


def _classify_text(text: str) -> Optional[Avail]:
    """
    Hilton-aware classifier (in priority order):
      1. WAF/error page → UNKNOWN (None) so caller can decide
      2. DATES_NOT_OPEN signals
      3. "rooms found" with N>0 → TWO_ROOMS
      4. Explicit "no rooms ..." copy (specific phrases) → NO_ROOMS
      5. Price + per-night signals → TWO_ROOMS
      6. Otherwise None (inconclusive)
    """
    t = text.lower()

    # WAF / generic Hilton error page — not a valid signal in either direction.
    waf_hits = len(_WAF_MARKER.findall(t))
    if waf_hits >= 1 and "rooms found" not in t and "select a room" not in t:
        return None

    if any(s in t for s in _NOT_OPEN):
        return Avail.DATES_NOT_OPEN

    m = _ROOMS_FOUND.search(t)
    if m and int(m.group(1)) > 0:
        return Avail.TWO_ROOMS

    if any(s in t for s in _NO_ROOMS):
        return Avail.NO_ROOMS

    price_hits = len(_PRICE_PATTERN.findall(text))
    night_hits = len(_PER_NIGHT.findall(text))
    if price_hits >= 2 and night_hits >= 2:
        return Avail.TWO_ROOMS

    return None


# ── JSON-blob helpers ─────────────────────────────────────────────────────────

def _extract_json_blobs(html: str) -> list[dict]:
    candidates: list[dict] = []
    for pat in [
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});?\s*</script>',
        r'window\.__APP_STATE__\s*=\s*(\{.+?\});?\s*</script>',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                candidates.append(json.loads(m.group(1)))
            except (json.JSONDecodeError, ValueError):
                pass
    return candidates


def _classify_json(data: dict) -> Optional[Avail]:
    """
    Conservative classifier: only return a definitive answer when the JSON
    clearly contains real room/rate data. Top-level empty arrays are
    inconclusive (Next.js __NEXT_DATA__ has empty placeholder fields).
    """
    rooms = (
        data.get("rooms")
        or data.get("roomTypes")
        or data.get("rates")
        or data.get("roomRates")
        or []
    )
    if not isinstance(rooms, list) or len(rooms) == 0:
        return None

    bookable = [
        r for r in rooms
        if isinstance(r, dict) and (
            r.get("availableRooms", 0) > 0
            or r.get("inventory", 0) > 0
            or r.get("rate")
            or r.get("price")
        )
    ]
    if len(bookable) >= 2:
        return Avail.TWO_ROOMS
    if len(bookable) == 1:
        return Avail.ONE_ROOM
    return None  # don't say NO_ROOMS from JSON; let text classifier decide


# ── API approach ──────────────────────────────────────────────────────────────

def check_via_api(arrival: date, departure: date) -> Optional[Avail]:
    session = get_session()
    url = booking_url(arrival, departure, num_rooms=2)
    try:
        resp = session.get(
            url, timeout=20,
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
    except requests.RequestException as exc:
        log.debug("API request error: %s", exc)
        return None

    if resp.status_code == 404:
        return Avail.DATES_NOT_OPEN
    if resp.status_code != 200:
        return None

    if "application/json" in resp.headers.get("Content-Type", ""):
        try:
            r = _classify_json(resp.json())
            if r is not None:
                return r
        except ValueError:
            pass

    html = resp.text
    for blob in _extract_json_blobs(html):
        r = _classify_json(blob)
        if r is not None:
            return r

    return _classify_text(html)


# ── Browser fallback (patchright + persistent Chrome profile) ─────────────────
#
# Hilton's Akamai WAF detects standard Playwright Chrome. patchright +
# real Chrome (channel="chrome") + a persistent profile + the
# AutomationControlled flag disabled gets past it. We run "headed" but
# position the window off-screen so it's invisible to the user.

BROWSER_PROFILE = Path(os.environ.get(
    "HILTON_PROFILE",
    os.path.expanduser("~/.hilton-monitor-profile"),
))


def check_via_playwright(arrival: date, departure: date) -> Avail:
    try:
        from patchright.sync_api import sync_playwright
        from patchright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        log.error("patchright not installed. Run: pip install patchright && patchright install chromium")
        return Avail.UNKNOWN

    url = booking_url(arrival, departure, num_rooms=2)
    label = f"{arrival.isoformat()}_to_{departure.isoformat()}"

    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE),
            headless=False,                       # Akamai blocks headless
            channel="chrome",                     # real Chrome, not Chromium
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=-5000,-5000",  # off-screen, invisible
            ],
        )
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            log.warning("domcontentloaded timeout for %s", label)
            ctx.close()
            return Avail.UNKNOWN

        # Wait for any of the classification signals to appear. Saves time
        # vs. a fixed-duration sleep on fast loads.
        try:
            page.wait_for_function(
                """() => {
                    if (!document.body) return false;
                    const t = (document.body.innerText || '').toLowerCase();
                    // Wait for definitive page-state signal. Don't wait on
                    // the bare "no rooms" string — it appears inside hidden
                    // React components even on bookable pages.
                    return /\\d+\\s+rooms?\\s+found/.test(t)
                        || t.includes('hilton page reference code')
                        || t.includes('not available for booking')
                        || t.includes('outside the booking window')
                        || t.includes('we couldn\\'t find any rooms')
                        || t.includes('no rooms match');
                }""",
                timeout=20000,
            )
        except PWTimeout:
            # Fall through and classify whatever rendered.
            log.debug("no signal selector matched for %s", label)
            page.wait_for_timeout(3000)

        html = page.content()

        if DEBUG:
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / f"{label}.html").write_text(html, encoding="utf-8")
            try:
                page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
            except Exception as exc:
                log.warning("screenshot failed: %s", exc)
            log.info("  [DEBUG] artifacts saved to %s/%s.*", DEBUG_DIR, label)

        ctx.close()

    result = _classify_text(html)
    if result is not None:
        return result

    for blob in _extract_json_blobs(html):
        r = _classify_json(blob)
        if r is not None:
            return r

    return Avail.UNKNOWN


# ── Per-window check ──────────────────────────────────────────────────────────

def check_dates(arrival: date, departure: date) -> CheckResult:
    nights = (departure - arrival).days
    log.info("Checking %s → %s (%d nights) ...", arrival, departure, nights)

    avail = check_via_api(arrival, departure)
    source = "API"
    if avail is None or avail == Avail.UNKNOWN:
        log.info("  API inconclusive → falling back to Playwright")
        avail = check_via_playwright(arrival, departure)
        source = "Playwright"

    _labels = {
        Avail.DATES_NOT_OPEN: "dates NOT YET open for booking",
        Avail.NO_ROOMS:       "dates open, no rooms available",
        Avail.ONE_ROOM:       "AT LEAST 1 ROOM AVAILABLE",
        Avail.TWO_ROOMS:      "BOTH ROOMS AVAILABLE",
        Avail.UNKNOWN:        "could not determine",
    }
    log.info("  [%s] %s", source, _labels.get(avail, str(avail)))

    return CheckResult(arrival=arrival, departure=departure, avail=avail)


# ── Notification ──────────────────────────────────────────────────────────────

def send_notification(available: list[CheckResult]) -> None:
    if SKIP_NOTIFY:
        log.info("SKIP_NOTIFY set – not sending notification")
        return
    if not NTFY_TOPIC:
        log.warning("NTFY_TOPIC not set – skipping notification")
        return

    lines = ["Hampton Inn Lexington Historic District rooms available — book now!\n"]
    for r in available:
        nights = (r.departure - r.arrival).days
        two_flag = "YES" if r.avail == Avail.TWO_ROOMS else "UNCERTAIN – verify on booking page"
        lines.append(f"• {r.arrival} check-in  →  {r.departure} check-out  ({nights} nights)")
        lines.append(f"  2 rooms simultaneously: {two_flag}")
        lines.append(f"  Book: {booking_url(r.arrival, r.departure)}")
        lines.append("")

    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data="\n".join(lines).encode("utf-8"),
            headers={
                "Title":    f"[Hilton LXTSWHX] {len(available)} date window(s) bookable",
                "Priority": "high",
                "Tags":     "hotel,calendar,bell",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Notification sent (HTTP %d)", resp.status_code)
    except Exception as exc:
        log.error("Notification failed: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Hilton LXTSWHX availability monitor  [mode=%s]", MODE)
    log.info("Hotel  : Hampton Inn Lexington Historic District")
    log.info("Range  : %s for %d day(s)", CHECK_IN_START, CHECK_IN_DAYS)
    log.info("Stays  : %s nights  |  2 rooms, 2 adults each",
             ", ".join(map(str, STAY_LENGTHS)))
    log.info("Notify : %s", "skipped" if SKIP_NOTIFY else "enabled" if NTFY_TOPIC else "no topic")
    log.info("Debug  : %s", "ON" if DEBUG else "off")
    log.info("=" * 60)

    available: list[CheckResult] = []
    for offset in range(CHECK_IN_DAYS):
        arrival = CHECK_IN_START + timedelta(days=offset)
        for nights in STAY_LENGTHS:
            departure = arrival + timedelta(days=nights)
            result = check_dates(arrival, departure)
            if result.avail in (Avail.ONE_ROOM, Avail.TWO_ROOMS):
                available.append(result)
            time.sleep(REQUEST_DELAY)

    log.info("=" * 60)
    log.info("Scan complete. Available windows: %d", len(available))
    if available:
        send_notification(available)
    else:
        log.info("No rooms found – no notification.")


if __name__ == "__main__":
    main()
