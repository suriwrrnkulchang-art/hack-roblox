import copy
import json
import os
import secrets
import threading
import time
import urllib.request
import urllib.error

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
)
from waitress import serve


# ---------- ตั้งค่าระบบและความปลอดภัย ----------
ADMIN_PASSWORD = "6155045"  # รหัสผ่านเข้าหน้าเว็บของคุณ

# ---------- ตั้งค่าแมพ (ใช้ Place ID ทั้ง 2 แมพตามเดิม) ----------
MAPS = {
    "Place 1": "120651982896178",
    "Place 2": "77210125175879",
}

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8888))
LOCK = threading.RLock()

# ---------- ตั้งค่า Supabase ----------
SUPABASE_URL = "https://roiuyoflkmftbxtdeuax.supabase.co"
SUPABASE_KEY = "sb_secret_fIRNmp3YmHBbX9sANw3SUQ_FjTvju6I"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def load_from_supabase():
    try:
        url = f"{SUPABASE_URL}/rest/v1/control_state?select=*"
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data:
                places_data = {}
                token = None
                for row in data:
                    pid = row.get("place_id")
                    if pid == "__SYS_TOKEN__":
                        token = row.get("reason")
                    else:
                        places_data[pid] = {
                            "mode": row.get("mode", "open"),
                            "reason": row.get("reason", "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง"),
                            "deadline": row.get("deadline")
                        }
                return {"token": token or secrets.token_urlsafe(32), "places": places_data}
    except Exception as e:
        print("Load error:", e)
    return None

def save_to_supabase(place_id, state_data):
    try:
        payload = {
            "place_id": place_id,
            "mode": state_data.get("mode"),
            "reason": state_data.get("reason"),
            "deadline": state_data.get("deadline")
        }
        url = f"{SUPABASE_URL}/rest/v1/control_state"
        req = urllib.request.Request(
            url, 
            data=json.dumps(payload).encode(), 
            headers={**HEADERS, "Prefer": "resolution=merge-duplicates"}, 
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            pass
    except Exception as e:
        print("Save error:", e)

# โหลดข้อมูลจาก Supabase
DATA = load_from_supabase()
if not DATA or not DATA.get("places"):
    DATA = {
        "token": secrets.token_urlsafe(32),
        "places": {},
    }
    for place_id in MAPS.values():
        DATA["places"][place_id] = {
            "mode": "open",
            "reason": "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง",
            "deadline": None,
        }
        save_to_supabase(place_id, DATA["places"][place_id])
    save_to_supabase("__SYS_TOKEN__", {"mode": "token", "reason": DATA["token"], "deadline": None})

for place_id in MAPS.values():
    DATA["places"].setdefault(
        place_id,
        {
            "mode": "open",
            "reason": "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง",
            "deadline": None,
        },
    )


def reconcile_locked():
    now = time.time()
    changed = False
    for place_id, state in DATA["places"].items():
        if (
            state["mode"] == "scheduled"
            and state["deadline"] is not None
            and now >= state["deadline"]
        ):
            state["mode"] = "closed"
            state["deadline"] = None
            save_to_supabase(place_id, state)
            changed = True


def snapshot(place_id):
    with LOCK:
        reconcile_locked()
        result = copy.deepcopy(DATA["places"][place_id])
        result["serverNow"] = time.time()
        return result


def update_state(place_id, mode=None, reason=None, seconds=None):
    with LOCK:
        reconcile_locked()
        state = DATA["places"][place_id]
        
        if mode is not None:
            state["mode"] = mode
            if mode == "scheduled" and seconds is not None:
                state["deadline"] = time.time() + seconds
            elif mode != "scheduled":
                state["deadline"] = None
                
        if reason is not None:
            state["reason"] = reason
            
        save_to_supabase(place_id, state)


# ---------- Flask Web & API Server ----------
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# หน้าเว็บสำหรับกรอกรหัสผ่าน
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <title>Login - Roblox Control Center</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, sans-serif; background: #1e1e2f; color: #d1d1e0; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: #2a2a40; padding: 30px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.3); width: 100%; max-width: 350px; text-align: center; }
        h2 { color: #fff; margin-bottom: 20px; }
        input { width: 100%; padding: 10px; margin-bottom: 15px; background: #1e1e2f; border: 1px solid #3f3f5f; color: #fff; border-radius: 6px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #7692ff; border: none; border-radius: 6px; font-weight: bold; color: #fff; cursor: pointer; }
        button:hover { opacity: 0.9; }
        .error { color: #ff4d4d; font-size: 14px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>🔒 เข้าสู่ระบบ</h2>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="password" name="password" placeholder="กรอกรหัสผ่านแอดมิน" required>
            <button type="submit">เข้าสู่ระบบ</button>
        </form>
    </div>
</body>
</html>
"""

# หน้าเว็บควบคุมหลัก (Web Dashboard)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Roblox Control Center</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #1e1e2f; color: #d1d1e0; margin: 0; padding: 20px; display: flex; justify-content: center; }
        .container { width: 100%; max-width: 600px; background: #2a2a40; padding: 25px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
        h2 { color: #ffffff; margin-top: 0; border-bottom: 2px solid #3f3f5f; padding-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
        .logout-btn { font-size: 12px; background: #ff4d4d; padding: 5px 10px; border-radius: 4px; color: #fff; text-decoration: none; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; color: #7692ff; font-weight: bold; }
        select, input[type="text"] { width: 100%; padding: 10px; background: #1e1e2f; border: 1px solid #3f3f5f; color: #fff; border-radius:
