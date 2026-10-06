import copy
import json
import os
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request
from waitress import serve


# ---------- ตั้งค่าแมพ (ใช้ Place ID ทั้ง 2 แมพตามเดิม) ----------
MAPS = {
    "(Place 1)": "120651982896178",
    "(Place 2)": "77210125175879",
}

HOST = "0.0.0.0"  # ต้องตั้งเป็น 0.0.0.0 สำหรับบน Cloud
PORT = int(os.environ.get("PORT", 8888))  # ดึง Port ตามที่ Render กำหนดให้อัตโนมัติ

DATA_FILE = Path(__file__).resolve().with_name("controller_state.json")
LOCK = threading.RLock()


# ---------- ระบบจัดการไฟล์สถานะ ----------
def save_locked():
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(DATA, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, DATA_FILE)


if DATA_FILE.exists():
    DATA = json.loads(DATA_FILE.read_text(encoding="utf-8"))
else:
    DATA = {
        "token": secrets.token_urlsafe(32),
        "places": {},
    }

for place_id in MAPS.values():
    DATA["places"].setdefault(
        place_id,
        {
            "mode": "open",
            "reason": "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง",
            "deadline": None,
        },
    )

with LOCK:
    save_locked()


def reconcile_locked():
    now = time.time()
    changed = False
    for state in DATA["places"].values():
        if (
            state["mode"] == "scheduled"
            and state["deadline"] is not None
            and now >= state["deadline"]
        ):
            state["mode"] = "closed"
            state["deadline"] = None
            changed = True
    if changed:
        save_locked()


def snapshot(place_id):
    with LOCK:
        reconcile_locked()
        result = copy.deepcopy(DATA["places"][place_id])
        result["serverNow"] = time.time()
        return result


# ---------- Flask API Server ----------
app = Flask(__name__)


@app.get("/")
def home():
    return "Roblox Controller Backend is running!", 200


@app.get("/state/<place_id>")
def get_state(place_id):
    expected = "Bearer " + DATA["token"]
    supplied = request.headers.get("Authorization", "")

    if not secrets.compare_digest(supplied, expected):
        return jsonify({"error": "unauthorized"}), 401

    if place_id not in MAPS.values():
        return jsonify({"error": "unknown place"}), 404

    response = jsonify(snapshot(place_id))
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    print(f"API Token ของคุณคือ: {DATA['token']}")
    serve(app, host=HOST, port=PORT, threads=8)
