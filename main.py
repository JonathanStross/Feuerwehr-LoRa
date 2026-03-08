#!/usr/bin/env python3
import os
import time
import json
import base64
import csv
import threading
from datetime import datetime, timezone

import requests
from pubsub import pub
import meshtastic.serial_interface

# -----------------------
# Config
# -----------------------
CSV_PATH = os.getenv("CSV_PATH", "/data/nachrichten.csv")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TARGET_CHANNEL_NAME = os.getenv("TARGET_CHANNEL_NAME", "Feuerwehr")
TARGET_CHANNEL_INDEX = int(os.getenv("TARGET_CHANNEL_INDEX", "1"))  # bei dir: Feuerwehr = 1

SERIAL_DEV = os.getenv("MESHTASTIC_DEV", "/dev/ttyUSB0")

DEBUG_PACKETS = os.getenv("DEBUG_PACKETS", "0") == "1"

HEALTHCHECK_INTERVAL_SEC = int(os.getenv("HEALTHCHECK_INTERVAL_SEC", "3600"))

TELEGRAM_API_BASE = "https://api.telegram.org"

# -----------------------
# Globals / State
# -----------------------
CHANNEL_MAP = {}

CSV_LOCK = threading.Lock()

# Cache: frische Position pro Node
LAST_POS = {}  # sender_id -> (lat, lon, time)

# Cache: letzte Telemetrie vom Gateway (local node)
LAST_DEVICE_METRICS = {}  # keys: batteryLevel, voltage, channelUtilization, airUtilTx, uptimeSeconds, time
LAST_LOCAL_STATS = {}     # keys: numPacketsTx, numPacketsRx, numOnlineNodes, heapFreeBytes, heapTotalBytes, ...
LAST_TELEMETRY_TS = None  # epoch seconds of telemetry packet

GATEWAY_NODE_ID = "unknown"


# -----------------------
# Helpers
# -----------------------
def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def json_fallback_encoder(obj):
    if isinstance(obj, (bytes, bytearray)):
        b64 = base64.b64encode(bytes(obj)).decode("ascii")
        return {"__bytes_b64__": b64}
    return str(obj)

def safe_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=json_fallback_encoder)

def tg_api(method: str, token: str, params: dict | None = None) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    r = requests.post(url, data=params or {}, timeout=10)
    try:
        return r.json()
    except Exception:
        r.raise_for_status()
        raise

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] Token/ChatID fehlt – Telegram deaktiviert.")
        return
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    res = tg_api("sendMessage", TELEGRAM_BOT_TOKEN, payload)
    if not res.get("ok"):
        print("[telegram] sendMessage fehlgeschlagen:", safe_json(res))

def validate_telegram_config() -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt – Telegram ist deaktiviert.")
        return False

    me = tg_api("getMe", TELEGRAM_BOT_TOKEN)
    if not me.get("ok"):
        print("[telegram] Token ungültig:", safe_json(me))
        return False
    print(f"[telegram] Bot ok: @{me['result'].get('username')}")

    chat = tg_api("getChat", TELEGRAM_BOT_TOKEN, {"chat_id": TELEGRAM_CHAT_ID})
    if not chat.get("ok"):
        print("[telegram] Chat-ID ungültig/kein Zugriff:", safe_json(chat))
        return False

    title = chat["result"].get("title") or chat["result"].get("username")
    print(f"[telegram] Chat ok: type={chat['result'].get('type')}, title={title}")
    return True

def safe_get(d: dict, *keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default

def get_channel_map(interface) -> dict:
    ch_map = {}
    try:
        for idx, ch in interface.localNode.channels.items():
            name = None
            if isinstance(ch, dict):
                name = (ch.get("settings") or {}).get("name") or ch.get("name")
            else:
                name = getattr(getattr(ch, "settings", None), "name", None) or getattr(ch, "name", None)
            if name:
                ch_map[int(idx)] = str(name)
    except Exception:
        pass
    return ch_map

def get_gateway_node_id(interface) -> str:
    try:
        ln = getattr(interface, "localNode", None)
        node_id = getattr(ln, "nodeId", None)
        if node_id:
            return str(node_id)
        node_num = getattr(ln, "nodeNum", None)
        if node_num is not None:
            return str(node_num)
    except Exception:
        pass
    return "unknown"

def channel_matches(packet: dict) -> bool:
    ch_index = packet.get("channel")
    if CHANNEL_MAP:
        ch_name = CHANNEL_MAP.get(ch_index)
        return ch_name == TARGET_CHANNEL_NAME
    return ch_index == TARGET_CHANNEL_INDEX

def compute_hops(packet: dict):
    hop_start = safe_get(packet, "hopStart", "hop_start")
    hop_limit = safe_get(packet, "hopLimit", "hop_limit")
    if hop_start is None or hop_limit is None:
        return None
    try:
        hs = int(hop_start)
        hl = int(hop_limit)
        return hs - hl if hs >= hl else None
    except Exception:
        return None

def maps_link(lat, lon):
    if lat is None or lon is None:
        return None
    return f"https://maps.google.com/?q={lat},{lon}"

def rssi_badge(rssi):
    if rssi is None:
        return "❔"
    try:
        r = int(rssi)
    except Exception:
        return "❔"
    if r >= -90:
        return "🟢"
    if r >= -105:
        return "🟡"
    return "🔴"

def snr_badge(snr):
    if snr is None:
        return "❔"
    try:
        s = float(snr)
    except Exception:
        return "❔"
    if s >= 8:
        return "🟢"
    if s >= 3:
        return "🟡"
    return "🔴"

def ensure_csv_header():
    header = [
        "timestamp_utc",
        "geraeteId",
        "nachricht",
        "rssi",
        "snr",
        "lat",
        "long",
        "hops",
        "gatewayNode",
    ]
    try:
        with CSV_LOCK:
            try:
                with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
                    first = f.readline()
                    if first and "timestamp_utc" in first:
                        return
            except FileNotFoundError:
                pass

            os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
    except Exception as e:
        print("[csv] header init failed:", e)

def append_csv_row(row: dict):
    ensure_csv_header()
    with CSV_LOCK:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                row.get("timestamp_utc"),
                row.get("geraeteId"),
                row.get("nachricht"),
                row.get("rssi"),
                row.get("snr"),
                row.get("lat"),
                row.get("long"),
                row.get("hops"),
                row.get("gatewayNode"),
            ])

def update_pos_cache_from_packet(packet: dict):
    decoded = packet.get("decoded") or {}
    pos = decoded.get("position") or {}

    lat = pos.get("latitude")
    lon = pos.get("longitude")
    t = pos.get("time")

    if lat is None and "latitudeI" in pos:
        lat = pos.get("latitudeI")
    if lon is None and "longitudeI" in pos:
        lon = pos.get("longitudeI")

    if isinstance(lat, int):
        lat = lat / 1e7
    if isinstance(lon, int):
        lon = lon / 1e7

    if lat is None or lon is None:
        return

    sender_id = packet.get("fromId") or str(packet.get("from", "unknown"))
    LAST_POS[sender_id] = (lat, lon, t)

def extract_lat_lon(interface, sender_id: str):
    # 1) bevorzugt Cache (frischste empfangene POSITION_APP)
    if sender_id in LAST_POS:
        lat, lon, _t = LAST_POS[sender_id]
        return lat, lon

    # 2) Fallback NodeDB
    node_info = interface.nodes.get(sender_id) or {}
    pos = node_info.get("position") or {}

    lat = pos.get("latitude")
    lon = pos.get("longitude")

    if lat is None and "latitudeI" in pos:
        lat = pos.get("latitudeI")
    if lon is None and "longitudeI" in pos:
        lon = pos.get("longitudeI")

    if isinstance(lat, int):
        lat = lat / 1e7
    if isinstance(lon, int):
        lon = lon / 1e7

    return lat, lon


# -----------------------
# Health status formatting
# -----------------------
def infer_power_source(battery_level, voltage):
    """
    Meshtastic 'deviceMetrics' hat nicht immer ein klares Feld für "USB vs Batterie".
    In der Praxis sieht man oft:
      - batteryLevel == 101 bei extern/USB (häufiges Meshtastic-Verhalten)
    Wir reporten das als "Indiz", nicht als 100% Tatsache.
    """
    if battery_level is None and voltage is None:
        return "unbekannt"

    try:
        bl = int(battery_level) if battery_level is not None else None
    except Exception:
        bl = None

    # Häufig: 101 == "powered/charging/unknown but external"
    if bl == 101:
        return "vermutlich USB/extern (batteryLevel=101)"

    # Grobe Heuristik
    if voltage is not None:
        try:
            v = float(voltage)
            if v >= 4.35:
                return "vermutlich USB/extern (hohe Spannung)"
            if v <= 4.15:
                return "vermutlich Batterie"
        except Exception:
            pass

    return "unbekannt"

def build_health_message():
    dm = LAST_DEVICE_METRICS or {}
    ls = LAST_LOCAL_STATS or {}

    battery = dm.get("batteryLevel")
    voltage = dm.get("voltage")
    uptime = dm.get("uptimeSeconds")
    chan_util = dm.get("channelUtilization")
    air_tx = dm.get("airUtilTx")

    power_src = infer_power_source(battery, voltage)

    num_rx = ls.get("numPacketsRx")
    num_tx = ls.get("numPacketsTx")
    online = ls.get("numOnlineNodes")
    total = ls.get("numTotalNodes")
    dupe = ls.get("numRxDupe")
    relay = ls.get("numTxRelay")
    relay_cancel = ls.get("numTxRelayCanceled")
    heap_total = ls.get("heapTotalBytes")
    heap_free = ls.get("heapFreeBytes")

    last_tel_age = None
    if LAST_TELEMETRY_TS:
        last_tel_age = int(time.time() - LAST_TELEMETRY_TS)

    lines = [
        f"🩺 Health-Check (Gateway) [{TARGET_CHANNEL_NAME}]",
        f"🏁 Gateway: {GATEWAY_NODE_ID}",
        f"🕒 Zeit (UTC): {now_utc_iso()}",
        "",
        "🔋 Energie / Batterie:",
        f"- Batterie-Level: {battery}",
        f"- Spannung: {voltage} V",
        f"- Quelle: {power_src}",
        "",
        "📶 Funk / Auslastung:",
        f"- Channel Utilization: {chan_util}",
        f"- Air Util TX: {air_tx}",
        "",
        "⏱️ System:",
        f"- Uptime (s): {uptime}",
        f"- Letzte Telemetrie vor: {last_tel_age}s" if last_tel_age is not None else "- Letzte Telemetrie: (keine Daten)",
        "",
        "📊 Mesh Stats:",
        f"- Online Nodes: {online} / Total: {total}",
        f"- Pakete RX/TX: {num_rx} / {num_tx}",
        f"- RX Dupe: {dupe}",
        f"- TX Relay: {relay} (canceled: {relay_cancel})",
        "",
        "🧠 Speicher:",
        f"- Heap Free/Total: {heap_free} / {heap_total}",
    ]

    # Unnötige "None"-Werte nicht zu hässlich wirken lassen
    msg = "\n".join(lines)
    msg = msg.replace("None", "—")
    return msg


def healthcheck_loop():
    """
    Sendet beim Start einmal, dann alle HEALTHCHECK_INTERVAL_SEC Sekunden.
    """
    # beim Start
    try:
        send_telegram(build_health_message())
    except Exception as e:
        print("[health] start send failed:", e)

    while True:
        time.sleep(HEALTHCHECK_INTERVAL_SEC)
        try:
            send_telegram(build_health_message())
        except Exception as e:
            print("[health] periodic send failed:", e)


# -----------------------
# Meshtastic callbacks
# -----------------------
def on_connection(interface, topic=pub.AUTO_TOPIC):
    global CHANNEL_MAP, GATEWAY_NODE_ID
    CHANNEL_MAP = get_channel_map(interface)
    GATEWAY_NODE_ID = get_gateway_node_id(interface)

    print("Connected. Channels:", CHANNEL_MAP)
    print("Gateway node:", GATEWAY_NODE_ID)
    print(f"Filter: name='{TARGET_CHANNEL_NAME}' (fallback index={TARGET_CHANNEL_INDEX})")


def on_receive(packet, interface):
    global LAST_DEVICE_METRICS, LAST_LOCAL_STATS, LAST_TELEMETRY_TS

    if DEBUG_PACKETS:
        print("[debug] packet:", safe_json(packet))

    decoded = packet.get("decoded") or {}
    portnum = decoded.get("portnum")

    # Positionscache
    if portnum == "POSITION_APP":
        update_pos_cache_from_packet(packet)
        return

    # Telemetrie: Cache für Health-Check
    if portnum == "TELEMETRY_APP":
        tel = decoded.get("telemetry") or {}
        dm = tel.get("deviceMetrics") or {}
        ls = tel.get("localStats") or {}

        # Nur aktualisieren, wenn irgendwas drin ist
        if dm:
            LAST_DEVICE_METRICS = dm
            LAST_DEVICE_METRICS["time"] = tel.get("time")
            LAST_TELEMETRY_TS = int(time.time())
        if ls:
            LAST_LOCAL_STATS = ls
            LAST_LOCAL_STATS["time"] = tel.get("time")
            LAST_TELEMETRY_TS = int(time.time())

        return

    # Nur Textmessages in "Feuerwehr"
    text = decoded.get("text")
    if not text:
        return

    if not channel_matches(packet):
        return

    sender_id = packet.get("fromId") or str(packet.get("from", "unknown"))
    rssi = safe_get(packet, "rxRssi", "rx_rssi")
    snr = safe_get(packet, "rxSnr", "rx_snr")
    hops = compute_hops(packet)

    lat, lon = extract_lat_lon(interface, sender_id)

    # CSV schreiben
    try:
        append_csv_row({
            "timestamp_utc": now_utc_iso(),
            "geraeteId": sender_id,
            "nachricht": text,
            "rssi": rssi,
            "snr": snr,
            "lat": lat,
            "long": lon,
            "hops": hops,
            "gatewayNode": GATEWAY_NODE_ID,
        })
    except Exception as e:
        print("[csv] append failed:", e)

    # Telegram posten
    link = maps_link(lat, lon)
    link_line = f"🗺️ Standort: {link}" if link else "🗺️ Standort: (keine GPS-Daten)"

    tg_msg = (
        f"📡 Meshtastic [{TARGET_CHANNEL_NAME}]\n"
        f"🏁 Gateway: {GATEWAY_NODE_ID}\n"
        f"📟 Sender: {sender_id}\n"
        f"💬 Nachricht: {text}\n"
        f"📶 Empfang: RSSI {rssi} dBm {rssi_badge(rssi)} | "
        f"SNR {snr} dB {snr_badge(snr)} | Hops: {hops}\n"
        f"{link_line}\n"
        f"📍 GPS(Sender): {lat},{lon}"
    )
    send_telegram(tg_msg)


# -----------------------
# Main
# -----------------------
if __name__ == "__main__":
    ensure_csv_header()

    telegram_ok = validate_telegram_config()
    if telegram_ok:
        send_telegram("✅ Gateway gestartet und Telegram-Verbindung geprüft.")

    # Subscribe to events
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection, "meshtastic.connection.established")

    # Connect
    iface = meshtastic.serial_interface.SerialInterface(devPath=SERIAL_DEV)

    # initial channel map + gateway id
    CHANNEL_MAP = get_channel_map(iface)
    GATEWAY_NODE_ID = get_gateway_node_id(iface)

    # Health loop thread starten (startet sofort mit 1x Healthcheck)
    t = threading.Thread(target=healthcheck_loop, daemon=True)
    t.start()

    # keep alive
    while True:
        time.sleep(60)