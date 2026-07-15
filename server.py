from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
import sqlite3
import threading
import time
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# ─── Telegram credentials ──────────────────────────────────────
BOT_TOKEN = "7971224857:AAFSrm4pxw9IRlyoqMa2AY_T6Pf5F0aMyc0"
CHAT_ID   = "-5450778403"
# ───────────────────────────────────────────────────────────────
CURB_LAT, CURB_LNG = 4.3810, 100.9720

def is_raining():
    """Check Open-Meteo for current rainfall at the curb. Returns True/False/None."""
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?"
               f"latitude={CURB_LAT}&longitude={CURB_LNG}"
               f"&current=rain,precipitation&timezone=auto")
        r = requests.get(url, timeout=10)
        c = r.json().get("current", {})
        return (c.get("rain", 0) or 0) > 0 or (c.get("precipitation", 0) or 0) > 0
    except Exception:
        return None  # weather unknown
# ─── Rate limiting ─────────────────────────────────────────────
ALERT_COOLDOWN_MINUTES = 5
last_alert_time        = {}
# ───────────────────────────────────────────────────────────────

# ─── Silence control ───────────────────────────────────────────
silence_until = None
# ───────────────────────────────────────────────────────────────

# ─── Fake curbs ────────────────────────────────────────────────
FAKE_CURBS = [
    {
        "id"        : "Curb #2",
        "location"  : "Chancellor Hall",
        "lat"       : 4.3853,
        "lng"       : 100.9737,
        "state"     : "PRE_CLOG",
        "depth_cm"  : 2.5,
        "battery_v" : 3.7,
        "timestamp" : "2026-07-13 08:15:00"
    },
    {
        "id"        : "Curb #3",
        "location"  : "Block 11",
        "lat"       : 4.3840,
        "lng"       : 100.9755,
        "state"     : "CRITICAL",
        "depth_cm"  : 4.5,
        "battery_v" : 3.6,
        "timestamp" : "2026-07-13 08:20:00"
    },
    {
        "id"        : "Curb #4",
        "location"  : "R&D Assembly Point",
        "lat"       : 4.3870,
        "lng"       : 100.9750,
        "state"     : "NORMAL",
        "depth_cm"  : 0.1,
        "battery_v" : 4.0,
        "timestamp" : "2026-07-13 08:18:00"
    },
    {
        "id"        : "Curb #5",
        "location"  : "Masjid An-Nur",
        "lat"       : 4.3825,
        "lng"       : 100.9730,
        "state"     : "NORMAL",
        "depth_cm"  : 0.3,
        "battery_v" : 3.8,
        "timestamp" : "2026-07-13 08:10:00"
    }
]
# ───────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect("curb_data.db")
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            distance  REAL,
            depth     REAL,
            state     TEXT,
            battery   REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            state     TEXT,
            depth     REAL,
            message   TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_reading(distance, depth, state, battery):
    conn = sqlite3.connect("curb_data.db")
    c    = conn.cursor()
    c.execute("""
        INSERT INTO readings (timestamp, distance, depth, state, battery)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          distance, depth, state, battery))
    conn.commit()
    conn.close()

def save_alert(state, depth, message):
    conn = sqlite3.connect("curb_data.db")
    c    = conn.cursor()
    c.execute("""
        INSERT INTO alerts (timestamp, state, depth, message)
        VALUES (?, ?, ?, ?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          state, depth, message))
    conn.commit()
    conn.close()

def get_latest_reading():
    conn = sqlite3.connect("curb_data.db")
    c    = conn.cursor()
    c.execute("SELECT * FROM readings ORDER BY id DESC LIMIT 1")
    row  = c.fetchone()
    conn.close()
    return row

def get_history():
    conn = sqlite3.connect("curb_data.db")
    c    = conn.cursor()
    c.execute("""
        SELECT timestamp, depth, state
        FROM readings
        ORDER BY id DESC LIMIT 50
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def get_recent_alerts():
    conn = sqlite3.connect("curb_data.db")
    c    = conn.cursor()
    c.execute("""
        SELECT timestamp, state, depth, message
        FROM alerts
        ORDER BY id DESC LIMIT 10
    """)
    rows = c.fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM SEND
# ═══════════════════════════════════════════════════════════════

def send_telegram(message):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id"    : CHAT_ID,
        "text"       : message,
        "parse_mode" : "HTML"
    }
    response = requests.post(url, json=payload)
    return response.json()

def is_rate_limited(state):
    if state not in last_alert_time:
        return False
    diff = (datetime.now() - last_alert_time[state]).total_seconds() / 60
    return diff < ALERT_COOLDOWN_MINUTES

def is_silenced():
    global silence_until
    if silence_until and datetime.now() < silence_until:
        return True
    silence_until = None
    return False


# ═══════════════════════════════════════════════════════════════
#  TWO-WAY BOT — COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════

last_update_id  = 0
processed_ids   = set()

def init_bot_offset():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

        # Fetch every single pending update with no limit
        res     = requests.get(url, timeout=10)
        data    = res.json()
        results = data.get("result", [])

        if results:
            last_update_id = results[-1]["update_id"]
            # Acknowledge ALL of them in one shot
            requests.get(url, params={"offset": last_update_id + 1}, timeout=10)
            print(f"Cleared {len(results)} pending updates — bot starts fresh")
        else:
            print("No pending updates — bot ready")

    except Exception as e:
        print(f"Could not init bot offset: {e}")

def handle_commands():
    global last_update_id, silence_until

    while True:
        try:
            url    = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 3}
            res    = requests.get(url, params=params, timeout=10)
            data   = res.json()

            for update in data.get("result", []):
                update_id      = update["update_id"]
                last_update_id = update_id

                # Skip already processed updates
                if update_id in processed_ids:
                    continue
                processed_ids.add(update_id)

                message = update.get("message", {})
                text    = message.get("text", "").strip().lower()
                chat_id = str(message.get("chat", {}).get("id", ""))

                # Only respond to our group
                if chat_id != CHAT_ID:
                    continue

                # ── /status ───────────────────────────────────
                if text == "/status":
                    row = get_latest_reading()
                    if row:
                        state   = row[4]
                        depth   = row[3]
                        battery = row[5]
                        ts      = row[1]
                        emoji   = "✅" if state == "NORMAL" else "⚠️" if state == "PRE_CLOG" else "🚨"
                        reply   = (
                            f"📊 <b>CURB #1 STATUS</b>\n"
                            f"State      : {emoji} {state.replace('_', '-')}\n"
                            f"Water Depth: {depth} cm\n"
                            f"Battery    : {battery}V\n"
                            f"Last Updated: {ts}\n"
                            f"Alerts     : {'🔕 Silenced' if is_silenced() else '🔔 Active'}"
                        )
                    else:
                        reply = "📊 No data received yet — Arduino not connected."
                    send_telegram(reply)

                # ── /silence ──────────────────────────────────
                elif text.startswith("/silence"):
                    parts   = text.split()
                    minutes = 30  # default
                    if len(parts) > 1 and parts[1].isdigit():
                        minutes = int(parts[1])
                    silence_until = datetime.now() + timedelta(minutes=minutes)
                    send_telegram(
                        f"🔕 <b>Alerts silenced for {minutes} minutes</b>\n"
                        f"Will resume at: "
                        f"{silence_until.strftime('%H:%M:%S')}\n"
                        f"Type /unsilence to resume immediately."
                    )

                # ── /unsilence ────────────────────────────────
                elif text == "/unsilence":
                    silence_until = None
                    send_telegram("🔔 <b>Alerts resumed.</b> All notifications are active again.")

                # ── /help ─────────────────────────────────────
                elif text == "/help":
                    send_telegram(
                        "🤖 <b>CurbAlerts Bot — Available Commands</b>\n\n"
                        "/status — Get current water depth and state\n"
                        "/silence 30 — Mute alerts for 30 minutes\n"
                        "/unsilence — Turn alerts back on\n"
                        "/help — Show this message"
                    )

        except Exception as e:
            print(f"Bot polling error: {e}")

        time.sleep(3)


# ═══════════════════════════════════════════════════════════════
#  ROUTES — ARDUINO DATA INTAKE
# ═══════════════════════════════════════════════════════════════

@app.route("/alert", methods=["POST"])
def alert():
    data     = request.get_json()
    state    = data.get("state",       "UNKNOWN")
    depth    = data.get("depth_cm",    0)
    distance = data.get("distance_cm", 0)
    battery  = data.get("battery_v",   0)
    curb_id  = data.get("id",          "Curb #1")

    maps_link = "https://maps.google.com/?q=4.3810,100.9720"

    save_reading(distance, depth, state, battery)

    if state == "NORMAL":
        message = (
            f"✅ <b>ALL CLEAR</b>\n"
            f"Curb       : {curb_id}\n"
            f"Water Depth: {depth} cm\n"
            f"Status     : Drain is clear\n"
            f"📍 Location: {maps_link}"
        )
    elif state == "PRE_CLOG":
        rain = is_raining()
        if rain is False:
            cause = "No rainfall detected — likely BLOCKAGE. Send crew to clean this curb."
        elif rain is True:
            cause = "Rainfall ongoing — water rise expected, monitor closely."
        else:
            cause = "Debris accumulating — schedule maintenance"
        message = (
            f"⚠️ <b>PRE-CLOG WARNING</b>\n"
            f"Curb       : {curb_id}\n"
            f"Water Depth: {depth} cm\n"
            f"Status     : {cause}\n"
            f"📍 Location: {maps_link}"
        )
    elif state == "CRITICAL":
        rain = is_raining()
        if rain is False:
            cause = "DRAIN FULL with NO rainfall — severe blockage, dispatch cleaning crew NOW"
        elif rain is True:
            cause = "DRAIN FULL during rainfall — flood risk, immediate action required"
        else:
            cause = "DRAIN FULL — immediate action required"
        message = (
            f"🚨 <b>CRITICAL FLOOD ALERT</b>\n"
            f"Curb       : {curb_id}\n"
            f"Water Depth: {depth} cm\n"
            f"Status     : {cause}\n"
            f"📍 Location: {maps_link}"
        )
    else:
        message = f"❓ Unknown state received: {state}"e
    if not is_silenced() and not is_rate_limited(state):
        send_telegram(message)
        save_alert(state, depth, message)
        last_alert_time[state] = datetime.now()
        telegram_sent = True

    return jsonify({"status": "received", "telegram_sent": telegram_sent})


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data    = request.get_json()
    battery = data.get("battery_v", 0)
    curb_id = data.get("id",        "Curb #1")

    message = (
        f"💓 <b>DAILY HEARTBEAT</b>\n"
        f"Curb    : {curb_id}\n"
        f"Battery : {battery}V\n"
        f"Status  : Device online and functioning normally"
    )
    send_telegram(message)
    return jsonify({"status": "heartbeat sent"})


@app.route("/test", methods=["GET"])
def test():
    result = send_telegram("🔧 <b>Server is online and connected to Telegram.</b>")
    return jsonify({"status": "test sent", "telegram_response": result})


# ═══════════════════════════════════════════════════════════════
#  ROUTES — DASHBOARD API
# ═══════════════════════════════════════════════════════════════

@app.route("/api/curbs", methods=["GET"])
def api_curbs():
    row = get_latest_reading()
    real_curb = {
        "id"        : "Curb #1",
        "location"  : "Village 5G",
        "lat"       : 4.3810,
        "lng"       : 100.9720,
        "state"     : row[4]  if row else "NORMAL",
        "depth_cm"  : row[3]  if row else 0.0,
        "battery_v" : row[5]  if row else 0.0,
        "timestamp" : row[1]  if row else "No data yet",
        "real"      : True
    }
    all_curbs = [real_curb]
    for c in FAKE_CURBS:
        entry = dict(c)
        entry["real"] = False
        all_curbs.append(entry)
    return jsonify(all_curbs)


@app.route("/api/latest", methods=["GET"])
def api_latest():
    row = get_latest_reading()
    if row:
        return jsonify({
            "id"        : row[0],
            "timestamp" : row[1],
            "distance"  : row[2],
            "depth"     : row[3],
            "state"     : row[4],
            "battery"   : row[5]
        })
    return jsonify({"status": "no data yet"})


@app.route("/api/history", methods=["GET"])
def api_history():
    rows    = get_history()
    history = [{"timestamp": r[0], "depth": r[1], "state": r[2]} for r in rows]
    return jsonify(history)


@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    rows   = get_recent_alerts()
    alerts = [{"timestamp": r[0], "state": r[1],
               "depth": r[2], "message": r[3]} for r in rows]
    return jsonify(alerts)


# ═══════════════════════════════════════════════════════════════
#  ROUTES — PAGES
# ═══════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/curb/<curb_id>", methods=["GET"])
def curb_detail(curb_id):
    return render_template("curb.html", curb_id=curb_id)


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════

init_db()

# Initialise offset then start bot polling
init_bot_offset()
bot_thread = threading.Thread(target=handle_commands, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)