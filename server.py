from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ─── Your Telegram credentials ───────────────────────────────────────────────
BOT_TOKEN = "7971224857:AAFSrm4pxw9IRlyoqMa2AY_T6Pf5F0aMyc0"
CHAT_ID   = "-5450778403"
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    response = requests.post(url, json=payload)
    return response.json()

@app.route("/alert", methods=["POST"])
def alert():
    data = request.get_json()

    state     = data.get("state", "UNKNOWN")
    depth     = data.get("depth_cm", 0)
    battery   = data.get("battery_v", 0)
    curb_id   = data.get("id", "Curb #1")

    if state == "NORMAL":
        message = (
            f"✅ <b>ALL CLEAR</b>\n"
            f"Curb       : {curb_id}\n"
            f"Water Depth: {depth} cm\n"
            f"Status     : Drain is clear\n"
            f"Battery    : {battery}V"
        )

    elif state == "PRE_CLOG":
        message = (
            f"⚠️ <b>PRE-CLOG WARNING</b>\n"
            f"Curb       : {curb_id}\n"
            f"Water Depth: {depth} cm\n"
            f"Status     : Debris accumulating — schedule maintenance\n"
            f"Battery    : {battery}V"
        )

    elif state == "CRITICAL":
        message = (
            f"🚨 <b>CRITICAL FLOOD ALERT</b>\n"
            f"Curb       : {curb_id}\n"
            f"Water Depth: {depth} cm\n"
            f"Status     : DRAIN FULL — immediate action required\n"
            f"Battery    : {battery}V"
        )

    else:
        message = f"❓ Unknown state received: {state}"

    result = send_telegram(message)
    return jsonify({"status": "sent", "telegram_response": result})


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data    = request.get_json()
    battery = data.get("battery_v", 0)
    curb_id = data.get("id", "Curb #1")

    message = (
        f"💓 <b>DAILY HEARTBEAT</b>\n"
        f"Curb    : {curb_id}\n"
        f"Battery : {battery}V\n"
        f"Status  : Device online and functioning normally"
    )

    result = send_telegram(message)
    return jsonify({"status": "sent", "telegram_response": result})


@app.route("/test", methods=["GET"])
def test():
    result = send_telegram("🔧 <b>Server is online and connected to Telegram.</b>")
    return jsonify({"status": "test sent", "telegram_response": result})


if __name__ == "__main__":
    app.run(debug=True, port=5000)