import serial
import requests
import time

PORT = "COM5"
SERVER = "https://flashstopcurb.onrender.com"

PRE_CLOG = 1.5
CRITICAL = 3.0

STABLE_COUNT = 3  # consecutive readings needed to confirm a state change

def get_state(depth):
    if depth >= CRITICAL:
        return "CRITICAL"
    elif depth >= PRE_CLOG:
        return "PRE_CLOG"
    return "NORMAL"

def main():
    print(f"Connecting to Arduino on {PORT}...")
    ser = serial.Serial(PORT, 9600, timeout=5)
    time.sleep(2)
    print("Connected! Forwarding data to server...")

    last_sent = 0
    last_state = None
    candidate_state = None
    candidate_count = 0

    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line.startswith("DEPTH:"):
            continue
        try:
            depth = float(line.split(":")[1])
        except ValueError:
            continue

        raw_state = get_state(depth)
        print(f"Depth: {depth} cm | Reading: {raw_state}")

        # debounce: new state must hold for STABLE_COUNT consecutive readings
        if raw_state == candidate_state:
            candidate_count += 1
        else:
            candidate_state = raw_state
            candidate_count = 1

        if candidate_count >= STABLE_COUNT:
            confirmed = candidate_state
        else:
            confirmed = last_state

        if confirmed is None:
            confirmed = raw_state

        # send when confirmed state changes, or every 60s to keep dashboard fresh
        if confirmed != last_state or time.time() - last_sent >= 60:
            try:
                r = requests.post(f"{SERVER}/alert", json={
                    "id": "Curb #1",
                    "state": confirmed,
                    "depth_cm": depth,
                    "distance_cm": 5.0 - depth,
                    "battery_v": 0
                }, timeout=60)
                print(f"  -> State {confirmed} sent: {r.status_code}")
                last_sent = time.time()
                last_state = confirmed
            except Exception as e:
                print(f"  -> Send failed: {e}")

if __name__ == "__main__":
    main()