"""Voi bike notifier v2 — stateful, with smart deduplication."""

import os
import re
import sys
import json
import requests
from math import radians, sin, cos, asin, sqrt
from urllib.parse import unquote
from datetime import datetime, timezone, timedelta

# --- Config ---
HOME_LAT = float(os.environ["HOME_LAT"])
HOME_LON = float(os.environ["HOME_LON"])
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
GIST_ID = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]

SEARCH_RADIUS_M = 300
MIN_BATTERY_PCT = 40
MAX_RANGE_KM = 80
HOLD_DURATION_MIN = 10
WINDOW_GAP_MIN = 20
CLOSER_THRESHOLD_PCT = 10

FEED_URL = (
    "https://api.voiapp.io/gbfs/be/"
    "7a4cb689-a2ee-409d-a5fe-49d2e8d50717/v2/free_bike_status.json"
)
GIST_API = f"https://api.github.com/gists/{GIST_ID}"


# --- Time helpers ---
def now_utc():
    return datetime.now(timezone.utc)

def to_iso(dt):
    return dt.isoformat()

def from_iso(s):
    return datetime.fromisoformat(s)


# --- Geometry & URI helpers ---
def distance_meters(lat1, lon1, lat2, lon2):
    R = 6_371_000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))

def extract_deep_link(rental_uri):
    m = re.search(r"adj_deep_link=([^&]+)", rental_uri)
    return unquote(m.group(1)) if m else rental_uri

def extract_code(rental_uri):
    m = re.search(r"voiapp%3A%2F%2Fscooter%2F([a-z0-9]+)", rental_uri)
    return m.group(1).upper() if m else "?"


# --- Gist state I/O ---
def load_state():
    r = requests.get(GIST_API, headers={"Authorization": f"Bearer {GIST_TOKEN}"}, timeout=10)
    r.raise_for_status()
    content = r.json()["files"]["state.json"]["content"]
    return json.loads(content) if content.strip() else {}

def save_state(state):
    body = {"files": {"state.json": {"content": json.dumps(state, indent=2)}}}
    r = requests.patch(
        GIST_API,
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json=body,
        timeout=10,
    )
    r.raise_for_status()


# --- Feed parsing ---
def fetch_bikes():
    """Return all available bikes near home, sorted by distance."""
    r = requests.get(FEED_URL, timeout=10)
    r.raise_for_status()
    bikes = r.json()["data"]["bikes"]

    results = []
    for b in bikes:
        if b.get("vehicle_type_id") != "voi_bike":
            continue
        if b["is_reserved"] or b["is_disabled"]:
            continue
        range_km = b.get("current_range_meters", 0) / 1000
        battery_pct = range_km / MAX_RANGE_KM * 100
        if battery_pct < MIN_BATTERY_PCT:
            continue
        d = distance_meters(HOME_LAT, HOME_LON, b["lat"], b["lon"])
        if d > SEARCH_RADIUS_M:
            continue
        results.append({
            "code": extract_code(b["rental_uris"]["android"]),
            "distance_m": d,
            "range_km": range_km,
            "battery_pct": battery_pct,
            "deep_link": extract_deep_link(b["rental_uris"]["android"]),
        })

    results.sort(key=lambda x: x["distance_m"])
    return results

def find_bike_in_feed_raw(code):
    """Look up a bike by code in the raw feed (regardless of available/reserved/etc).
    Returns the raw bike dict or None."""
    r = requests.get(FEED_URL, timeout=10)
    r.raise_for_status()
    for b in r.json()["data"]["bikes"]:
        if extract_code(b["rental_uris"]["android"]) == code:
            return b
    return None


# --- Notifications ---
def send_notification(title, body, click_url=None, tag="bike"):
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": tag,
    }
    if click_url:
        headers["Click"] = click_url
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()

def bike_message(bike):
    walk_min = max(1, round(bike["distance_m"] / 80))
    return (
        f"Bike: {bike['code']}\n"
        f"Distance: {bike['distance_m']:.0f}m (~{walk_min} min walk)\n"
        f"Battery: ~{bike['battery_pct']:.0f}% ({bike['range_km']:.0f} km range)"
    )


# --- Decision logic ---
def is_new_window(state):
    if not state.get("window_started_at"):
        return True
    started = from_iso(state["window_started_at"])
    return (now_utc() - started) > timedelta(minutes=WINDOW_GAP_MIN)

def record_alerted(state, bike):
    state["alerted_bike"] = {
        "code": bike["code"],
        "distance_m": bike["distance_m"],
        "alerted_at": to_iso(now_utc()),
    }
    state["reservation_alert_sent"] = False
    return state


def run():
    state = load_state()
    bikes = fetch_bikes()
    current_best = bikes[0] if bikes else None

    # --- Case: new window ---
    if is_new_window(state):
        new_state = {"window_started_at": to_iso(now_utc())}
        if current_best:
            send_notification(
                f"🚲 Reserve {current_best['code']}",
                bike_message(current_best) + "\n💡 Reserve in app when 10 min away",
                click_url=current_best["deep_link"],
            )
            new_state = record_alerted(new_state, current_best)
        else:
            send_notification(
                "⚠️ No bikes nearby",
                "No Voi bikes available near home right now.\nConsider walking or transit.",
            )
            new_state["alerted_bike"] = None
            new_state["reservation_alert_sent"] = False
        save_state(new_state)
        print("New window. Done.")
        return

    # --- Case: continuing window ---
    alerted = state.get("alerted_bike")

    # No alert ever made this window (we sent "no bikes" earlier). Try again if bikes appeared.
    if alerted is None:
        if current_best:
            send_notification(
                f"🚲 Reserve {current_best['code']}",
                bike_message(current_best) + "\n💡 Reserve in app when 10 min away",
                click_url=current_best["deep_link"],
            )
            state = record_alerted(state, current_best)
            save_state(state)
            print(f"Bike appeared mid-window: {current_best['code']}")
        else:
            print("Still no bikes. Silent.")
        return

    # Look up the alerted bike's current status
    raw = find_bike_in_feed_raw(alerted["code"])
    alerted_at = from_iso(alerted["alerted_at"])
    age = now_utc() - alerted_at

    if raw is None:
        # Vanished
        if current_best:
            send_notification(
                f"🚲 {alerted['code']} gone — try {current_best['code']}",
                bike_message(current_best) + "\n💡 Reserve in app when 10 min away",
                click_url=current_best["deep_link"],
            )
            state = record_alerted(state, current_best)
        else:
            send_notification(
                f"⚠️ {alerted['code']} gone, no replacement",
                "No other Voi bikes available nearby right now.",
            )
            state["alerted_bike"] = None
            state["reservation_alert_sent"] = False
        save_state(state)
        return

    if raw["is_reserved"]:
        if not state.get("reservation_alert_sent"):
            body = f"{alerted['code']} is now reserved."
            click = None
            if current_best and current_best["code"] != alerted["code"]:
                body += "\n\nIf that wasn't you, next best:\n" + bike_message(current_best)
                click = current_best["deep_link"]
            send_notification(f"🔒 {alerted['code']} reserved", body, click_url=click)
            state["reservation_alert_sent"] = True
            if current_best and current_best["code"] != alerted["code"]:
                state = record_alerted(state, current_best)
                state["reservation_alert_sent"] = True  # don't re-alert immediately for the new one
            save_state(state)
            print(f"Reservation alert sent for {alerted['code']}")
        else:
            print(f"{alerted['code']} still reserved; alert already sent. Silent.")
        return

    # Alerted bike still available
    if age > timedelta(minutes=HOLD_DURATION_MIN):
        bike_to_alert = current_best  # may be same as alerted, may be different
        send_notification(
            f"🚲 Reserve {bike_to_alert['code']} (hold expired)",
            bike_message(bike_to_alert) + "\n💡 Re-reserve in app when 10 min away",
            click_url=bike_to_alert["deep_link"],
        )
        state = record_alerted(state, bike_to_alert)
        save_state(state)
        print(f"10 min passed; re-alerted for {bike_to_alert['code']}")
        return

    # Check for meaningfully closer bike
    if current_best and current_best["code"] != alerted["code"]:
        improvement = (alerted["distance_m"] - current_best["distance_m"]) / alerted["distance_m"]
        if improvement >= CLOSER_THRESHOLD_PCT / 100:
            send_notification(
                f"🚲 Closer bike: {current_best['code']}",
                bike_message(current_best) + f"\n(Previous: {alerted['code']} at {alerted['distance_m']:.0f}m)",
                click_url=current_best["deep_link"],
            )
            state = record_alerted(state, current_best)
            save_state(state)
            print(f"Closer bike: {current_best['code']}")
            return

    print(f"{alerted['code']} still best, nothing new. Silent.")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)