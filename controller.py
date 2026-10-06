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

# โหลดข้อมูลเริ่มต้นจาก Supabase
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
        select, input[type="text"] { width: 100%; padding: 10px; background: #1e1e2f; border: 1px solid #3f3f5f; color: #fff; border-radius: 6px; box-sizing: border-box; }
        .reason-box { display: flex; gap: 10px; }
        .reason-box input { flex: 1; }
        .btn-save-reason { background: #0984e3; white-space: nowrap; padding: 0 15px; cursor: pointer; border: none; border-radius: 6px; font-weight: bold; color: #fff; }
        .btn-save-reason:hover { opacity: 0.9; }
        .checkbox-group { display: flex; align-items: center; gap: 10px; margin: 15px 0; }
        .btn-container { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 20px; }
        button { padding: 10px 18px; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; color: #fff; font-size: 14px; flex: 1; min-width: 120px; }
        .btn-close { background: #ff4d4d; }
        .btn-cancel { background: #ffa502; }
        .btn-open { background: #00b894; }
        .btn-close-all { background: #d63031; width: 100%; margin-top: 10px; }
        .btn-open-all { background: #00cec9; width: 100%; margin-top: 10px; }
        .btn-cancel-all { background: #e17055; width: 100%; margin-top: 10px; }
        button:hover { opacity: 0.9; }
        .status-box { background: #1e1e2f; padding: 15px; border-radius: 6px; margin-top: 20px; border-left: 5px solid #00ffcc; }
        .token-box { margin-top: 20px; font-size: 12px; word-break: break-all; background: #15151f; padding: 10px; border-radius: 4px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>🛡️ Roblox Control Center <a href="/logout" class="logout-btn">ออกจากระบบ</a></h2>
        <div class="form-group">
            <label>เลือกแมพ:</label>
            <select id="mapSelect" onchange="loadMapState()">
                <option value="ALL">🌐 จัดการทั้งหมด (ทุกแมพ)</option>
                {% for name, pid in maps.items() %}
                <option value="{{ pid }}">{{ name }}</option>
                {% endfor %}
            </select>
        </div>
        <div class="form-group">
            <label>เหตุผล / ข้อความเตะ:</label>
            <div class="reason-box">
                <input type="text" id="reasonInput" value="เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง">
                <button type="button" class="btn-save-reason" onclick="saveReasonOnly()">💾 บันทึกข้อความ</button>
            </div>
        </div>
        <div class="checkbox-group">
            <input type="checkbox" id="timerEnabled" checked onchange="toggleTimerInput()">
            <label for="timerEnabled" style="margin:0; color:#d1d1e0;">นับถอยหลังก่อนปิด</label>
        </div>
        <div class="form-group" id="secondsGroup">
            <label>เวลา (วินาที):</label>
            <input type="text" id="secondsInput" value="60">
        </div>
        <div class="btn-container">
            <button class="btn-close" onclick="sendAction('scheduled')">🛑 เริ่มปิด (แมพที่เลือก)</button>
            <button class="btn-cancel" onclick="sendAction('open-cancel')">↩️ ยกเลิก (แมพที่เลือก)</button>
            <button class="btn-open" onclick="sendAction('open')">✅ เปิดแมพ (แมพที่เลือก)</button>
        </div>
        <div class="btn-container" style="flex-direction: column; gap: 5px;">
            <button class="btn-close-all" onclick="sendActionAll('scheduled')">🚨 ปิดเซิร์ฟเวอร์ทั้งหมดทันที (ทุกแมพ)</button>
            <button class="btn-cancel-all" onclick="sendActionAll('open-cancel')">↩️ ยกเลิกการปิดทั้งหมด (ทุกแมพ)</button>
            <button class="btn-open-all" onclick="sendActionAll('open')">✅ เปิดให้บริการทั้งหมด (ทุกแมพ)</button>
        </div>
        <div class="status-box" id="statusView">กำลังโหลดสถานะ...</div>
        <div class="token-box"><b>API Token (สำหรับใส่ในสคริปต์ Roblox):</b><br>{{ token }}</div>
    </div>

    <script>
        async function loadMapState() {
            const pid = document.getElementById('mapSelect').value;
            if(pid === 'ALL') {
                document.getElementById('statusView').innerText = "สถานะ: กำลังควบคุมทุกแมพพร้อมกัน";
                return;
            }
            const res = await fetch('/api/state/' + pid);
            if(res.ok) {
                const data = await res.json();
                document.getElementById('reasonInput').value = data.reason;
                let modeText = data.mode === 'open' ? '🟢 เปิดให้บริการ' : (data.mode === 'scheduled' ? '⏳ กำลังนับถอยหลัง' : '🔴 ปิดปรับปรุง');
                document.getElementById('statusView').innerText = "สถานะ: " + modeText;
            }
        }
        function toggleTimerInput() {
            const enabled = document.getElementById('timerEnabled').checked;
            document.getElementById('secondsGroup').style.display = enabled ? 'block' : 'none';
        }

        async function saveReasonOnly() {
            const pid = document.getElementById('mapSelect').value;
            const reason = document.getElementById('reasonInput').value;

            const res = await fetch('/api/save-reason', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({place_id: pid, reason: reason})
            });
            if(res.ok) {
                alert('บันทึกข้อความ/เหตุผลสำเร็จ!');
            } else {
                alert('เกิดข้อผิดพลาดในการบันทึกข้อความ');
            }
        }

        async function sendAction(actionType) {
            const pid = document.getElementById('mapSelect').value;
            if(pid === 'ALL') {
                alert('กรุณาใช้ปุ่มควบคุมทั้งหมดด้านล่างสำหรับการสั่งการทุกแมพครับ');
                return;
            }
            const reason = document.getElementById('reasonInput').value;
            const seconds = document.getElementById('secondsInput').value;
            let mode = 'open';
            if(actionType === 'scheduled') mode = document.getElementById('timerEnabled').checked ? 'scheduled' : 'closed';
            if(actionType === 'open') mode = 'open';
            if(actionType === 'open-cancel') mode = 'open';

            const res = await fetch('/api/update', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({place_id: pid, mode: mode, reason: reason, seconds: parseInt(seconds) || 60})
            });
            if(res.ok) {
                alert('บันทึกคำสั่งสำเร็จ!');
                loadMapState();
            } else {
                alert('เกิดข้อผิดพลาด');
            }
        }

        async function sendActionAll(actionType) {
            let confirmMsg = 'คุณแน่ใจหรือไม่ที่จะดำเนินการกับ "ทุกแมพ" พร้อมกัน?';
            if(actionType === 'scheduled') confirmMsg = 'คุณแน่ใจหรือไม่ที่จะปิดเซิร์ฟเวอร์ "ทุกแมพ" พร้อมกัน?';
            if(actionType === 'open') confirmMsg = 'คุณแน่ใจหรือไม่ที่จะเปิดให้บริการ "ทุกแมพ" พร้อมกัน?';
            if(actionType === 'open-cancel') confirmMsg = 'คุณแน่ใจหรือไม่ที่จะยกเลิกการปิดของ "ทุกแมพ" พร้อมกัน?';

            if(!confirm(confirmMsg)) return;

            const reason = document.getElementById('reasonInput').value;
            const seconds = document.getElementById('secondsInput').value;
            let mode = 'open';
            if(actionType === 'scheduled') {
                mode = document.getElementById('timerEnabled').checked ? 'scheduled' : 'closed';
            } else {
                mode = 'open';
            }

            const res = await fetch('/api/update-all', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode: mode, reason: reason, seconds: parseInt(seconds) || 60})
            });
            if(res.ok) {
                alert('ส่งคำสั่งจัดการทุกแมพสำเร็จ!');
                loadMapState();
            } else {
                alert('เกิดข้อผิดพลาด');
            }
        }

        setInterval(loadMapState, 3000);
        loadMapState();
    </script>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            response = redirect("/")
            response.set_cookie("auth", ADMIN_PASSWORD)
            return response
        else:
            return render_template_string(LOGIN_HTML, error="รหัสผ่านไม่ถูกต้อง!")

    if request.cookies.get("auth") != ADMIN_PASSWORD:
        return render_template_string(LOGIN_HTML, error=None)

    return render_template_string(
        HTML_TEMPLATE, maps=MAPS, token=DATA["token"]
    )

@app.get("/logout")
def logout():
    response = redirect("/")
    response.set_cookie("auth", "", expires=0)
    return response

@app.get("/api/state/<place_id>")
def api_get_state(place_id):
    if place_id not in MAPS.values():
        return jsonify({"error": "unknown place"}), 404
    return jsonify(snapshot(place_id))

@app.post("/api/save-reason")
def api_save_reason():
    req = request.json
    pid = req.get("place_id")
    reason = req.get("reason")
    
    if pid == "ALL":
        for place_id in MAPS.values():
            update_state(place_id, reason=reason)
        return jsonify({"success": True})
        
    if pid not in MAPS.values():
        return jsonify({"error": "unknown place"}), 404
        
    update_state(pid, reason=reason)
    return jsonify({"success": True})

@app.post("/api/update")
def api_update():
    req = request.json
    pid = req.get("place_id")
    mode = req.get("mode")
    reason = req.get("reason")
    seconds = req.get("seconds")
    if pid not in MAPS.values():
        return jsonify({"error": "unknown place"}), 404
    update_state(pid, mode, reason, seconds)
    return jsonify({"success": True})

@app.post("/api/update-all")
def api_update_all():
    req = request.json
    mode = req.get("mode")
    reason = req.get("reason")
    seconds = req.get("seconds")
    
    for place_id in MAPS.values():
        update_state(place_id, mode, reason, seconds)
        
    return jsonify({"success": True})

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
    serve(app, host=HOST, port=PORT, threads=8)
