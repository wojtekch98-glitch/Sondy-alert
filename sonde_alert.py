import json
import math
import os
from datetime import datetime, timedelta, timezone
import requests

# === KONFIGURACJA ===
MY_LAT = 53.04            # Twoja szerokość geograficzna (wystarczą 2 miejsca po przecinku)
MY_LON = 18.61            # Twoja długość geograficzna
RADIUS_KM = 40.0          # Promień powiadomień w km
NTFY_TOPIC = "sondy_meteo_torun_wch"

API_URL = "https://api.v2.sondehub.org/sondes/telemetry"
STATE_FILE = "sonde_alerts_state.json"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_state(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cleaned = {}
    for serial, info in state.items():
        updated = info.get("updated_at")
        if not updated or datetime.fromisoformat(updated) > cutoff:
            cleaned[serial] = info
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)


def send_notification(title: str, body: str, map_url: str) -> None:
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": "high",
            "Tags": "parachute,radio",
            "Click": map_url,
        },
        timeout=15,
    )


def main() -> None:
    if os.getenv("SEND_TEST", "").lower() == "true":
        send_notification(
            title="Test SondeHub Alert",
            body=f"Połączenie działa! Monitoruję promień {RADIUS_KM} km.",
            map_url=f"https://www.google.com/maps?q={MY_LAT},{MY_LON}",
        )

    state = load_state()
    resp = requests.get(API_URL, params={"duration": "30m"}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    now = datetime.now(timezone.utc)

    for serial, packets in data.items():
        if not isinstance(packets, dict) or not packets:
            continue

        latest_ts = max(packets.keys())
        pkt = packets[latest_ts]
        lat, lon, alt = pkt.get("lat"), pkt.get("lon"), pkt.get("alt")
        if lat is None or lon is None or alt is None:
            continue

        dist_km = haversine_km(MY_LAT, MY_LON, float(lat), float(lon))
        if dist_km > RADIUS_KM:
            continue

        vel_v = pkt.get("vel_v")
        freq = pkt.get("frequency") or pkt.get("tx_frequency") or "brak danych"
        sonde_type = pkt.get("type", "Nieznany typ")
        pkt_time = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        age_sec = (now - pkt_time).total_seconds()

        sonde_state = state.get(serial, {"approaching_sent": False, "landed_sent": False})
        sonde_state["updated_at"] = now.isoformat()
        maps_link = f"https://www.google.com/maps?q={lat:.5f},{lon:.5f}"

        details = (
            f"Sonda: {serial} ({sonde_type})\n"
            f"Odległość: {dist_km:.1f} km\n"
            f"Wysokość: {alt:.0f} m n.p.m.\n"
            f"Częstotliwość: {freq} MHz\n"
            f"Pozycja: {lat:.5f}, {lon:.5f}"
        )

        # 1. Niskie opadanie (< 2000 m n.p.m.)
        is_descending = vel_v is not None and float(vel_v) < -0.8
        if alt < 2000 and is_descending and not sonde_state["approaching_sent"]:
            send_notification(
                title=f"Sonda schodzi do lądowania ({dist_km:.1f} km)",
                body=f"{details}\nPrędkość opadania: {vel_v:.1f} m/s",
                map_url=maps_link,
            )
            sonde_state["approaching_sent"] = True

        # 2. Lądowanie lub utrata sygnału poniżej 1000 m
        on_ground_vel = vel_v is not None and abs(float(vel_v)) < 0.6 and alt < 600
        signal_lost_low = alt < 1000 and age_sec > 180
        if (on_ground_vel or signal_lost_low) and not sonde_state["landed_sent"]:
            reason = (
                "Sonda spoczywa na ziemi (prędkość ~0 m/s)"
                if on_ground_vel
                else f"Utracono sygnał na niskiej wysokości ({int(age_sec // 60)} min temu)"
            )
            send_notification(
                title=f"WYLĄDOWAŁA SONDA: {serial} ({dist_km:.1f} km)",
                body=f"{reason}\n{details}",
                map_url=maps_link,
            )
            sonde_state["landed_sent"] = True

        state[serial] = sonde_state

    save_state(state)


if __name__ == "__main__":
    main()
