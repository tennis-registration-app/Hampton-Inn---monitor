#!/usr/bin/env python3
"""
Monitor Hampton Inn Lexington Historic District (LXTSWHX) for room availability.

Checks May 22–30, 2027 for 2/3/4 night stays (2 rooms, 2 adults each).
Sends a push notification via ntfy.sh when any rooms become bookable.

Failure-state semantics:
  DATES_NOT_OPEN – booking window hasn't opened yet   → no notification
  NO_ROOMS       – dates open but no inventory        → no notification
  ONE_ROOM / TWO_ROOMS – at least one room bookable   → notification sent
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum, auto
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Configuration ─────────────────────────────────────────────────────────────

CTYHOCN         = "LXTSWHX"
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC", "")

CHECK_IN_START  = date(2027, 5, 22)
CHECK_IN_DAYS   = 9          # May 22–30 inclusive
STAY_LENGTHS    = [2, 3, 4]
ADULTS_PER_ROOM = 2

REQUEST_DELAY   = 1.5        # polite seconds between checks

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Availability states ───────────────────────────────────────────────────────

class Avail(Enum):
    DATES_NOT_OPEN = auto()   # booking window not yet open
    NO_ROOMS       = auto()   # dates open, zero inventory
    ONE_ROOM       = auto()   # ≥1 room confirmed; 2nd room not confirmed
    TWO_ROOMS      = auto()   # both rooms confirmed simultaneously
    UNKNOWN        = auto()   # could not determine


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


# ── Text-classification signals ───────────────────────────────────────────────

_NOT_OPEN = [
    "not available for booking", "dates are not available",
    "not yet available", "outside the booking window",
    "maximum advance", "too far in advance",
    "advance purchase period", "dates unavailable",
    "cannot be booked", "no longer accepting",
]
_NO_ROOMS = [
    "no rooms available", "no availability", "sold out",
    "0 rooms", "no rates available", "no results",
    "no hotel rooms were found", "we couldn't find",
    "there are no rooms",
]
_AVAILABLE = [
    "per night", "select room", "book now", "view rate",
    "add to cart", "/night", "from $", "usd",
    "choose your room", "room details", "avg/night",
]


def _classify_text(text: str) -> Optional[Avail]:
    t = text.lower()
    if any(s in t for s in _NOT_OPEN):
        return Avail.DATES_NOT_OPEN
    if any(s in t for s in _AVAILABLE):
        return Avail.TWO_ROOMS   # optimistic; Playwright refines if needed
    if any(s in t for s in _NO_ROOMS):
        return Avail.NO_ROOMS
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
    s = json.dumps(data).lower()
    if "datesnotavailable" in s or "advancebooking" in s:
        return Avail.DATES_NOT_OPEN

    rooms = (
        data.get("rooms")
        or data.get("roomTypes")
        or data.get("rates")
        or data.get("roomRates")
        or []
    )
    if not isinstance(rooms, list):
        return None

    if len(rooms) == 0:
        if "notavailable" in s or "norooms" in s:
            return Avail.NO_ROOMS
        return None

    bookable = [
        r for r in rooms
        if isinstance(r, dict) and (
            r.get("availableRooms", 1) > 0
            or r.get("inventory", 1) > 0
            or "rate" in r
            or "price" in r
        )
    ]
    if len(bookable) >= 2:
        return Avail.TWO_ROOMS
    if len(bookable) == 1:
        return Avail.ONE_ROOM
    return Avail.NO_ROOMS


# ── API approach (preferred) ──────────────────────────────────────────────────

def check_via_api(arrival: date, departure: date) -> Optional[Avail]:
    """
    Attempt availability check via plain HTTP requests.
    Returns None when the response is inconclusive (SPA shell only).
    """
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
        log.debug("API returned HTTP %s", resp.status_code)
        return None

    if "application/json" in resp.headers.get("Content-Type", ""):
        try:
            result = _classify_json(resp.json())
            if result is not None:
                return result
        except ValueError:
            pass

    html = resp.text

    for blob in _extract_json_blobs(html):
        result = _classify_json(blob)
        if result is not None:
            return result

    return _classify_text(html)


# ── Playwright fallback ───────────────────────────────────────────────────────

def check_via_playwright(arrival: date, departure: date) -> Avail:
    """Headless Chromium fallback using Playwright + stealth."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        log.error(
            "Playwright not installed. "
            "Run: pip install playwright && playwright install chromium"
        )
        return Avail.UNKNOWN

    try:
        from playwright_stealth import stealth_sync
        has_stealth = True
    except ImportError:
        has_stealth = False
        log.warning("playwright-stealth not found; bot-detection evasion disabled")

    url = booking_url(arrival, departure, num_rooms=2)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = ctx.new_page()

        if has_stealth:
            stealth_sync(page)

        # ── Load the booking page ──────────────────────────────────────────
        try:
            page.goto(url, wait_until="networkidle", timeout=55000)
        except PWTimeout:
            log.warning("networkidle timeout; retrying with domcontentloaded")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(8000)
            except PWTimeout:
                log.warning("Page load timed out for %s → %s", arrival, departure)
                browser.close()
                return Avail.UNKNOWN

        # Try to extract JSON from the rendered DOM before falling back to text
        for blob in _extract_json_blobs(page.content()):
            result = _classify_json(blob)
            if result is not None:
                browser.close()
                return result

        html = page.content()
        browser.close()

    return _classify_text(html) or Avail.UNKNOWN


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
    if not NTFY_TOPIC:
        log.warning("NTFY_TOPIC not set – skipping notification")
        return

    lines = [
        "Hampton Inn Lexington Historic District rooms available — book now!\n",
    ]
    for r in available:
        nights = (r.departure - r.arrival).days
        two_flag = (
            "YES" if r.avail == Avail.TWO_ROOMS
            else "UNCERTAIN – verify on booking page"
        )
        lines.append(f"• {r.arrival} check-in  →  {r.departure} check-out  ({nights} nights)")
        lines.append(f"  2 rooms simultaneously: {two_flag}")
        lines.append(f"  Book: {booking_url(r.arrival, r.departure)}")
        lines.append("")

    body = "\n".join(lines)
    subject = f"[Hilton LXTSWHX] {len(available)} date window(s) bookable"

    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":    subject,
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
    log.info("Hilton LXTSWHX availability monitor")
    log.info("Hotel : Hampton Inn Lexington Historic District")
    log.info("Dates : May 22–30, 2027 check-in")
    log.info("Stays : 2 / 3 / 4 nights  |  2 rooms, 2 adults each")
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
    log.info("Scan complete. Available windows found: %d", len(available))

    if available:
        send_notification(available)
    else:
        log.info("No rooms found – no notification sent.")


if __name__ == "__main__":
    main()
