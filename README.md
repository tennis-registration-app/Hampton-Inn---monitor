# Hilton LXTSWHX Availability Monitor

Monitors **Hampton Inn Lexington Historic District** (Hilton property code `LXTSWHX`) for room availability across May 22–30, 2027 (2, 3, and 4 night stays, 2 rooms, 2 adults each).
Sends a push notification via [ntfy.sh](https://ntfy.sh) the moment any rooms are bookable.

## Architecture

Runs **locally on your Mac** via `launchd`. We tried GitHub Actions first but Hilton's Akamai WAF blocks datacenter IPs and headless Chrome fingerprints, so the cron lives on your machine instead.

- **Browser:** patchright (Playwright fork) driving **real Chrome** via `channel=chrome`
- **Profile:** persistent profile at `~/.hilton-monitor-profile` (built once, reused on every run)
- **Visibility:** Chrome launches with `--window-position=-5000,-5000` so the window is off-screen and invisible to you
- **Schedule:** `launchd` `StartInterval: 7200` (every 2 hours, with catch-up after sleep)
- **Notifications:** ntfy.sh topic `hilton-lxtswhx-x7n4q2k8m`

## Your private ntfy.sh topic

```
hilton-lxtswhx-x7n4q2k8m
```

Subscribed via the [ntfy mobile app](https://ntfy.sh/) (topic name + default server `https://ntfy.sh`).

## Local install (first time)

```bash
cd ~/Desktop/hilton-monitor

# 1. Python deps + a real-Chrome-aware Playwright fork
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/patchright install chromium

# 2. Install the launchd job (runs every 2 hours, persistent across reboots)
./install_launchd.sh
```

The install script asks for your `NTFY_TOPIC` and writes a populated plist to `~/Library/LaunchAgents/com.cswilliams.hilton-monitor.plist`.

## Operational commands

```bash
# Trigger an immediate run (don't wait for the next interval)
launchctl kickstart -k gui/$(id -u)/com.cswilliams.hilton-monitor

# Tail logs
tail -f ~/Desktop/hilton-monitor/logs/monitor.out.log

# Status (look for the launchd label)
launchctl list | grep hilton

# Uninstall
./install_launchd.sh uninstall
```

## Test mode

Useful when you want to verify the classifier without waiting for the production scan or risking a notification:

```bash
# Single known-bookable check, no notification
TEST_ARRIVAL=2026-08-15 TEST_NIGHTS=2 TEST_DAYS=1 SKIP_NOTIFY=1 \
  .venv/bin/python monitor.py

# Multi-day scan with debug artifacts saved to ./debug/
TEST_ARRIVAL=2026-08-15 TEST_NIGHTS=2,3,4 TEST_DAYS=3 SKIP_NOTIFY=1 DEBUG=1 \
  .venv/bin/python monitor.py
```

Env vars:

| Var            | Effect |
|----------------|--------|
| `TEST_ARRIVAL` | Override start date (YYYY-MM-DD) |
| `TEST_NIGHTS`  | Stay lengths (e.g. `2` or `2,3,4`) |
| `TEST_DAYS`    | Consecutive check-in days starting at `TEST_ARRIVAL` |
| `SKIP_NOTIFY`  | `1` = classify and log only, no ntfy POST |
| `DEBUG`        | `1` = save HTML + screenshot per check to `./debug/` |
| `NTFY_TOPIC`   | ntfy topic (omitted = skip notification) |
| `HILTON_PROFILE` | Override path to Chrome profile dir |

## Search parameters (production)

| Parameter | Value |
|-----------|-------|
| Hotel | Hampton Inn Lexington Historic District |
| Property code | `LXTSWHX` |
| Check-in dates | May 22, 23, 24, 25, 26, 27, 28, 29, 30 – 2027 |
| Stay lengths | 2, 3, or 4 nights |
| Rooms searched | 2 |
| Adults per room | 2 |
| Total combinations | 27 (9 dates × 3 lengths) |

## Notification example

```
[Hilton LXTSWHX] 2 date window(s) bookable

Hampton Inn Lexington Historic District rooms available — book now!

• 2027-05-24 check-in  →  2027-05-27 check-out  (3 nights)
  2 rooms simultaneously: YES
  Book: https://www.hilton.com/en/book/reservation/rooms/?ctyhocn=LXTSWHX&arrivalDate=2027-05-24&departureDate=2027-05-27&room1NumAdults=2&room2NumAdults=2
```

## Failure states (no notification sent)

| State | Meaning |
|-------|---------|
| `dates NOT YET open for booking` | Hilton's "Sorry, we don't have any rooms available" — typical for dates outside the ~13-month booking window |
| `dates open, no rooms available` | Search succeeded but all 10 room types sold out |
| `could not determine` | WAF block or network hiccup. Logged for visibility, not surfaced to user |

Only `ONE_ROOM` or `TWO_ROOMS` triggers a push notification.

## Repo layout

```
monitor.py                       # main script
requirements.txt                 # Python deps
install_launchd.sh               # launchd installer / uninstaller
com.cswilliams.hilton-monitor.plist  # launchd template (has placeholders)
.github/workflows/monitor.yml    # disabled GH Actions cron (kept for reference)
logs/                            # launchd stdout/stderr (created on install)
debug/                           # screenshots + HTML when DEBUG=1
```
