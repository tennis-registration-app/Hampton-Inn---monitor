# Hilton LXTSWHX Availability Monitor

Monitors **Hampton Inn Lexington Historic District** (Hilton property code `LXTSWHX`) for room availability across May 22–30, 2027 (2, 3, and 4 night stays, 2 rooms, 2 adults each).  
Sends a push notification via [ntfy.sh](https://ntfy.sh) the moment any rooms are bookable.

---

## Your private ntfy.sh topic

```
hilton-lxtswhx-x7n4q2k8m
```

Keep this string private — anyone who knows it can read (and send) notifications to it.

---

## Step 1 – Subscribe on your phone

| Platform | Link |
|----------|------|
| iOS | [App Store – ntfy](https://apps.apple.com/us/app/ntfy/id1625396347) |
| Android | [Google Play – ntfy](https://play.google.com/store/apps/details?id=io.heckel.ntfy) |
| Android (FOSS) | [F-Droid – ntfy](https://f-droid.org/en/packages/io.heckel.ntfy/) |
| Web browser | Visit `https://ntfy.sh/hilton-lxtswhx-x7n4q2k8m` and click **Subscribe** |

**Mobile steps:**
1. Install the ntfy app.
2. Tap **+** → **Add subscription**.
3. Set **Topic** to `hilton-lxtswhx-x7n4q2k8m` and the server to `https://ntfy.sh`.
4. Tap **Subscribe**.

You'll receive a high-priority push notification with booking links whenever rooms open up.

---

## Step 2 – Add the GitHub Actions secret

The monitor runs automatically every 2 hours via GitHub Actions.  
It needs your ntfy topic stored as a repository secret.

1. Open your repository on GitHub.
2. Go to **Settings → Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Set:
   - **Name:** `NTFY_TOPIC`
   - **Value:** `hilton-lxtswhx-x7n4q2k8m`
5. Click **Add secret**.

The workflow file is at [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml).

To run the monitor immediately (without waiting for the cron):  
**Actions** tab → **Hilton Availability Monitor** → **Run workflow**.

---

## Search parameters

| Parameter | Value |
|-----------|-------|
| Hotel | Hampton Inn Lexington Historic District |
| Property code | `LXTSWHX` |
| Check-in dates | May 22, 23, 24, 25, 26, 27, 28, 29, 30 – 2027 |
| Stay lengths | 2, 3, or 4 nights |
| Rooms searched | 2 |
| Adults per room | 2 |
| Total combinations | 27 (9 dates × 3 lengths) |

---

## Notification format

When at least one room is found, you'll receive a message like:

```
[Hilton LXTSWHX] 2 date window(s) bookable

Hampton Inn Lexington Historic District rooms available — book now!

• 2027-05-24 check-in  →  2027-05-27 check-out  (3 nights)
  2 rooms simultaneously: YES
  Book: https://www.hilton.com/en/book/reservation/rooms/?ctyhocn=LXTSWHX&arrivalDate=2027-05-24&departureDate=2027-05-27&room1NumAdults=2&room2NumAdults=2

• 2027-05-26 check-in  →  2027-05-28 check-out  (2 nights)
  2 rooms simultaneously: UNCERTAIN – verify on booking page
  Book: https://www.hilton.com/en/book/reservation/rooms/?ctyhocn=LXTSWHX&arrivalDate=2027-05-26&departureDate=2027-05-28&room1NumAdults=2&room2NumAdults=2
```

The "2 rooms simultaneously" flag is `YES` when the search (which explicitly requests 2 rooms) returns bookable inventory.  `UNCERTAIN` means 1 room type was detected but 2-room simultaneous availability couldn't be fully confirmed — click the link to verify before booking.

---

## Failure states (no notification sent)

| State | Meaning |
|-------|---------|
| `dates NOT YET open for booking` | Hilton's booking window hasn't reached May 2027 yet |
| `dates open, no rooms available` | The date window exists but no inventory remains |

Only a state of `ONE_ROOM` or `TWO_ROOMS` triggers a notification.

---

## How it works

1. **API approach (fast, preferred):** `monitor.py` fetches the Hilton booking page with plain HTTP and checks for server-side JSON state blobs (`__NEXT_DATA__`, etc.) and text signals. No browser needed.
2. **Playwright fallback:** If the API response is inconclusive (typical for fully client-side SPAs), a headless Chromium browser loads the page with `playwright-stealth` active to bypass bot-detection. The rendered HTML is then classified.

---

## Local testing

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run (replace topic or export it)
NTFY_TOPIC=hilton-lxtswhx-x7n4q2k8m python monitor.py
```

You can also send a test notification without running the full scan:

```bash
curl -d "Test from monitor" \
  -H "Title: Test notification" \
  https://ntfy.sh/hilton-lxtswhx-x7n4q2k8m
```
