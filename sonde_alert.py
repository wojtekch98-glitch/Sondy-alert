import json
import math
import os
from datetime import datetime, timedelta, timezone
import requests

# === KONFIGURACJA ===
MY_LAT = 53.04            # Szerokość geograficzna (Toruń)
MY_LON = 18.61            # Długość geograficzna (Toruń)
RADIUS_KM = 50.0          # Promień powiadomień (aktualna pozycja lub prognoza lądowania)
PREFILTER_KM = 250.0      # Zasięg wstępnego wyszukiwania sond w locie (do pobrania prognoz)
NTFY_TOPIC = "sondy_meteo_torun_wch"

TELEMETRY_URL = "https://api.v2.sondehub.org/sondes/telemetry"
PREDICTIONS_URL = "https://api.v2.sondehub.org/predictions"
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


def fetch_predictions(serials: list[str]) -> dict:
    """Pobiera prognozy lądowania z SondeHub dla podanej listy numerów seryjnych."""
    if not serials:
        return {}
    try:
        resp = requests.get(
            PREDICTIONS_URL,
            params={"vehicles": ",".join(serials)},
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()
    except (requests.RequestException, ValueError):
        return {}

    preds = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("vehicle"):
                preds[item["vehicle"]] = item
    elif isinstance(raw, dict):
        for key, item in raw.items():
            if isinstance(item, dict):
                preds[item.get("vehicle", key)] = item
    return preds


def extract_landing_point(pred: dict) -> tuple[float, float] | None:
    """Wyciąga współrzędne prognozowanego punktu przyziemienia z obiektu prognozy."""
    traj = pred.get("data")
    if isinstance(traj, str):
        try:
            traj = json.loads(traj)
        except json.JSONDecodeError:
            traj = None

    if isinstance(traj, list) and traj:
        last_pt = traj[-1]
        if isinstance(last_pt, dict):
            lat = last_pt.get("lat") if last_pt.get("lat") is not None else last_pt.get("latitude")
            lon = last_pt.get("lon") if last_pt.get("lon") is not None else last_pt.get("longitude")
            if lat is not None and lon is not None:
                return float(lat), float(lon)

    lat = pred.get("latitude") if pred.get("latitude") is not None else pred.get("lat")
    lon = pred.get("longitude") if pred.get("longitude") is not None else pred.get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon)

    return None


def main() -> None:
    if os.getenv("SEND_TEST", "").lower() == "true":
        send_notification(
            title="Test SondeHub Alert (z prognozą)",
            body=f"Połączenie działa! Monitoruję promień {RADIUS_KM} km (pozycja + prognoza po pęknięciu).",
            map_url=f"https://www.google.com/maps?q={MY_LAT},{MY_LON}",
        )

    state = load_state()
    resp = requests.get(TELEMETRY_URL, params={"duration": "30m"}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    now = datetime.now(timezone.utc)

    # Krok 1: Wstępna selekcja sond w promieniu 250 km
    candidates = {}
    for serial, packets in data.items():
        if not isinstance(packets, dict) or not packets:
            continue

        latest_ts = max(packets.keys())
        pkt = packets[latest_ts]
        lat, lon, alt = pkt.get("lat"), pkt.get("lon"), pkt.get("alt")
        if lat is None or lon is None or alt is None:
            continue

        dist_km = haversine_km(MY_LAT, MY_LON, float(lat), float(lon))
        if dist_km <= PREFILTER_KM:
            candidates[serial] = (latest_ts, pkt, dist_km)

    # Krok 2: Pobranie prognoz lądowania dla sond w regionie
    predictions = fetch_predictions(list(candidates.keys()))

    # Krok 3: Analiza alertów
    for serial, (latest_ts, pkt, dist_km) in candidates.items():
        lat = float(pkt["lat"])
        lon = float(pkt["lon"])
        alt = float(pkt["alt"])
        vel_v = pkt.get("vel_v")
        freq = pkt.get("frequency") or pkt.get("tx_frequency") or "brak danych"
        sonde_type = pkt.get("type", "Nieznany typ")
        pkt_time = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        age_sec = (now - pkt_time).total_seconds()

        sonde_state = state.get(
            serial,
            {
                "burst_pred_sent": False,
                "approaching_sent": False,
                "landed_sent": False,
            },
        )
        # Uzupełnienie klucza dla sond zapisanych przez starszą wersję skryptu
        sonde_state.setdefault("burst_pred_sent", False)
        sonde_state["updated_at"] = now.isoformat()

        current_maps_link = f"https://www.google.com/maps?q={lat:.5f},{lon:.5f}"
        pred = predictions.get(serial, {})
        pred_descending = bool(pred.get("descending"))
        is_burst_descending = (vel_v is not None and float(vel_v) < -1.5) or pred_descending

        # ALERT 1: Pęknięcie balonu + prognoza lądowania w promieniu 40 km
        if is_burst_descending and alt >= 1500 and not sonde_state["burst_pred_sent"]:
            landing_coords = extract_landing_point(pred)
            if landing_coords is not None:
                pred_lat, pred_lon = landing_coords
                pred_dist_km = haversine_km(MY_LAT, MY_LON, pred_lat, pred_lon)
                if pred_dist_km <= RADIUS_KM:
                    pred_maps_link = f"https://www.google.com/maps?q={pred_lat:.5f},{pred_lon:.5f}"
                    vel_str = f"{float(vel_v):.1f} m/s" if vel_v is not None else "brak danych"
                    send_notification(
                        title=f"PĘKŁ BALON! Prognoza lądowania: {pred_dist_km:.1f} km",
                        body=(
                            f"Sonda: {serial} ({sonde_type})\n"
                            f"Prognozowane lądowanie: {pred_dist_km:.1f} km od Ciebie\n"
                            f"Punkt prognozy: {pred_lat:.5f}, {pred_lon:.5f}\n"
                            f"Aktualna wysokość: {alt:.0f} m n.p.m. ({dist_km:.1f} km stąd)\n"
                            f"Opadanie: {vel_str} | Częstotliwość: {freq} MHz"
                        ),
                        map_url=pred_maps_link,
                    )
                    sonde_state["burst_pred_sent"] = True

        # Dalsze alerty dotyczą sond znajdujących się już fizycznie w promieniu 40 km
        if dist_km <= RADIUS_KM:
            details = (
                f"Sonda: {serial} ({sonde_type})\n"
                f"Odległość: {dist_km:.1f} km\n"
                f"Wysokość: {alt:.0f} m n.p.m.\n"
                f"Częstotliwość: {freq} MHz\n"
                f"Pozycja: {lat:.5f}, {lon:.5f}"
            )

            # ALERT 2: Niskie opadanie (< 2000 m n.p.m.)
            is_descending = vel_v is not None and float(vel_v) < -0.8
            if alt < 2000 and is_descending and not sonde_state["approaching_sent"]:
                send_notification(
                    title=f"Sonda schodzi do lądowania ({dist_km:.1f} km)",
                    body=f"{details}\nPrędkość opadania: {float(vel_v):.1f} m/s",
                    map_url=current_maps_link,
                )
                sonde_state["approaching_sent"] = True

            # ALERT 3: Lądowanie lub utrata sygnału poniżej 1000 m
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
                    map_url=current_maps_link,
                )
                sonde_state["landed_sent"] = True

        state[serial] = sonde_state

    save_state(state)


if __name__ == "__main__":
    main()
