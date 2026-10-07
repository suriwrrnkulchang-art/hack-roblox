"""
Roblox Control Center (เวอร์ชันเสถียร)

ตั้งค่าผ่าน Environment Variables (ห้ามใส่รหัสลงในโค้ด):
  ADMIN_PASSWORD   รหัสผ่านแอดมิน (จำเป็น)
  TOKEN_PIN        รหัสสำหรับดู/สร้าง Token (จำเป็น)
  SUPABASE_URL     เช่น https://xxxx.supabase.co   (ไม่ใส่ = ใช้ไฟล์ในเครื่องอย่างเดียว)
  SUPABASE_KEY     service key
  SECRET_KEY       (ไม่บังคับ) คีย์เซ็นต์ session
  LOCAL_FILE       (ไม่บังคับ) path ไฟล์สำรอง ค่าเริ่มต้น control_state.json
  COOKIE_SECURE    ตั้งเป็น 1 ถ้าใช้ HTTPS
  PORT             ค่าเริ่มต้น 8888
"""
import copy
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from functools import wraps

from flask import Flask, jsonify, redirect, render_template_string, request, session
from waitress import serve

# ---------------------------------------------------------------- ตั้งค่า
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "6155045")
TOKEN_PIN = os.environ.get("TOKEN_PIN", "991675788")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://roiuyoflkmftbxtdeuax.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_secret_WfO5ZgoulLDCVmTx_rZcMg_Q0zroFa1")
LOCAL_FILE = os.environ.get("LOCAL_FILE", "control_state.json")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8888))

if not ADMIN_PASSWORD or not TOKEN_PIN:
    raise SystemExit("กรุณาตั้ง ADMIN_PASSWORD และ TOKEN_PIN ใน environment ก่อนรัน")

MAPS = {
    "Place 1": "120651982896178",
    "Place 2": "77210125175879",
}
PID_TO_NAME = {pid: name for name, pid in MAPS.items()}

REMOTE = bool(SUPABASE_URL and SUPABASE_KEY)
TOKEN_KEY = "__SYS_TOKEN__"
DEFAULT_REASON = "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง"
MODES = ("open", "closed", "scheduled")
MIN_SEC, MAX_SEC, MAX_REASON = 1, 86400, 200

LOCK = threading.RLock()
WAKE = threading.Event()
DIRTY = set()  # key ที่ยังไม่ได้ซิงค์ขึ้น Supabase
LOG = deque(maxlen=60)
SYNC = {"remote_loaded": not REMOTE, "last_ok": None, "last_error": None, "extra_cols": True}
DATA = {"token": secrets.token_urlsafe(32), "token_updated_at": 0.0, "places": {}}


# ---------------------------------------------------------------- ตัวช่วย
def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def new_state():
    return {
        "mode": "open", "reason": DEFAULT_REASON, "deadline": None,
        "seconds": 60, "changed_at": time.time(), "updated_at": 0.0,
    }


def clean_state(raw):
    """ตรวจและแก้ข้อมูลจากไฟล์/Supabase ให้ถูกต้องเสมอ"""
    s = new_state()
    mode = raw.get("mode")
    s["mode"] = mode if mode in MODES else "open"
    s["reason"] = (str(raw.get("reason") or "").strip() or DEFAULT_REASON)[:MAX_REASON]
    s["seconds"] = min(MAX_SEC, max(MIN_SEC, int(num(raw.get("seconds"), 60))))
    s["deadline"] = None if raw.get("deadline") is None else num(raw.get("deadline"), None)
    if raw.get("changed_at") is not None:
        s["changed_at"] = num(raw.get("changed_at"), s["changed_at"])
    s["updated_at"] = num(raw.get("updated_at"))
    if s["mode"] == "scheduled" and s["deadline"] is None:
        s["mode"] = "closed"
    if s["mode"] != "scheduled":
        s["deadline"] = None
    return s


def add_log(pid, action, detail):
    LOG.append({"t": time.time(), "place": PID_TO_NAME.get(pid, "ทุกแมพ" if pid == "ALL" else pid),
                "action": action, "detail": detail})


# ---------------------------------------------------------------- ไฟล์สำรองในเครื่อง
def save_local():
    try:
        with LOCK:
            snap = {"token": DATA["token"], "token_updated_at": DATA["token_updated_at"],
                    "places": copy.deepcopy(DATA["places"]), "log": list(LOG)}
            tmp = LOCAL_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, LOCAL_FILE)  # เขียนแบบ atomic ไฟล์ไม่เสียแม้ดับกลางทาง
    except Exception as e:
        print("Local save error:", e)


def load_local():
    try:
        with open(LOCAL_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        with LOCK:
            if raw.get("token"):
                DATA["token"] = str(raw["token"])
                DATA["token_updated_at"] = num(raw.get("token_updated_at"))
            for pid in MAPS.values():
                if isinstance(raw.get("places", {}).get(pid), dict):
                    DATA["places"][pid] = clean_state(raw["places"][pid])
            for item in raw.get("log", []):
                if isinstance(item, dict):
                    LOG.append(item)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Local load error:", e)


# ---------------------------------------------------------------- Supabase
def sb(method, path, payload=None, prefer=None):
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SUPABASE_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        raise RuntimeError(str(e)[:200])


def build_row(key):
    if key == TOKEN_KEY:
        row = {"place_id": key, "mode": "token", "reason": DATA["token"], "deadline": None,
               "seconds": 60, "changed_at": None, "updated_at": DATA["token_updated_at"]}
    else:
        s = DATA["places"][key]
        row = {"place_id": key, "mode": s["mode"], "reason": s["reason"], "deadline": s["deadline"],
               "seconds": s["seconds"], "changed_at": s["changed_at"], "updated_at": s["updated_at"]}
    if not SYNC["extra_cols"]:
        row.pop("changed_at")
        row.pop("updated_at")
    return row


def push(key):
    with LOCK:
        DIRTY.discard(key)
    try:
        for attempt in range(2):
            with LOCK:
                row = build_row(key)
            try:
                sb("POST", "/rest/v1/control_state?on_conflict=place_id", row,
                   "resolution=merge-duplicates,return=minimal")
                break
            except RuntimeError as e:
                # ตารางยังไม่มีคอลัมน์ใหม่ -> ส่งแบบเดิมแทน (ดู SQL ท้ายไฟล์)
                if attempt == 0 and SYNC["extra_cols"] and ("changed_at" in str(e) or "updated_at" in str(e)):
                    SYNC["extra_cols"] = False
                    continue
                raise
        SYNC["last_ok"] = time.time()
        SYNC["last_error"] = None
    except Exception:
        with LOCK:
            DIRTY.add(key)
        raise


def merge_remote(rows):
    """รวมข้อมูลจาก Supabase: ใหม่กว่าชนะ และไม่เขียนทับของที่ยังไม่ได้ซิงค์"""
    with LOCK:
        seen = set()
        for row in rows:
            pid = row.get("place_id")
            r_up = num(row.get("updated_at"))
            if pid == TOKEN_KEY:
                seen.add(pid)
                if row.get("reason") and r_up >= DATA["token_updated_at"] and TOKEN_KEY not in DIRTY:
                    DATA["token"] = str(row["reason"])
                    DATA["token_updated_at"] = r_up
                elif r_up < DATA["token_updated_at"]:
                    DIRTY.add(TOKEN_KEY)
            elif pid in PID_TO_NAME:
                seen.add(pid)
                local = DATA["places"][pid]
                if r_up >= local["updated_at"] and pid not in DIRTY:
                    DATA["places"][pid] = clean_state(row)
                elif r_up < local["updated_at"]:
                    DIRTY.add(pid)
        for key in [TOKEN_KEY, *MAPS.values()]:
            if key not in seen:
                DIRTY.add(key)  # ฝั่ง Supabase ยังไม่มี -> ส่งขึ้นไป
    save_local()
    WAKE.set()


def try_remote_load():
    rows = sb("GET", "/rest/v1/control_state?select=*") or []
    merge_remote(rows)
    SYNC["remote_loaded"] = True
    SYNC["last_ok"] = time.time()
    SYNC["last_error"] = None


# ---------------------------------------------------------------- Logic หลัก
def reconcile_locked():
    now = time.time()
    for pid, s in DATA["places"].items():
        if s["mode"] == "scheduled" and s["deadline"] is not None and now >= s["deadline"]:
            s.update(mode="closed", changed_at=s["deadline"], updated_at=now, deadline=None)
            add_log(pid, "auto_close", "หมดเวลานับถอยหลัง ปิดแมพอัตโนมัติ")
            DIRTY.add(pid)
            save_local()
            WAKE.set()


def snapshot(pid):
    with LOCK:
        reconcile_locked()
        now = time.time()
        s = copy.deepcopy(DATA["places"][pid])
        s["serverNow"] = now
        s["remaining"] = max(0.0, s["deadline"] - now) if s["deadline"] else None
        s["elapsed"] = max(0.0, now - s["changed_at"])
        s["synced"] = SYNC["remote_loaded"]
        return s


def overview():
    with LOCK:
        reconcile_locked()
        now = time.time()
        places = {}
        for name, pid in MAPS.items():
            s = copy.deepcopy(DATA["places"][pid])
            s["name"] = name
            places[pid] = s
        return {
            "serverNow": now, "places": places, "log": list(LOG)[-25:][::-1],
            "sync": {"remote_enabled": REMOTE, "remote_loaded": SYNC["remote_loaded"],
                     "pending": len(DIRTY), "last_ok": SYNC["last_ok"],
                     "last_error": SYNC["last_error"], "extra_cols": SYNC["extra_cols"]},
        }


def update_state(pid, mode=None, reason=None, seconds=None):
    with LOCK:
        reconcile_locked()
        s = DATA["places"][pid]
        now = time.time()
        notes = []
        if reason is not None and reason != s["reason"]:
            s["reason"] = reason
            notes.append("แก้ข้อความ")
        if seconds is not None and seconds != s["seconds"]:
            s["seconds"] = seconds
            notes.append(f"ตั้งเวลา {seconds} วิ")
        if mode == "cancel":  # ยกเลิกเฉพาะที่กำลังนับถอยหลัง
            mode = "open" if s["mode"] == "scheduled" else None
        if mode is not None:
            if mode == "scheduled" or mode != s["mode"]:
                s["changed_at"] = now
            s["mode"] = mode
            s["deadline"] = now + s["seconds"] if mode == "scheduled" else None
            notes.append({"open": "เปิดแมพ", "closed": "ปิดแมพทันที",
                          "scheduled": f"เริ่มนับถอยหลัง {s['seconds']} วิ"}[mode])
        if notes:
            s["updated_at"] = now
            add_log(pid, "update", " · ".join(notes))
            DIRTY.add(pid)
            save_local()  # เซฟลงไฟล์ทันที ก่อนตอบกลับ
            WAKE.set()


def sync_worker():
    backoff = 1
    while True:
        WAKE.wait(timeout=1.0)
        WAKE.clear()
        try:
            with LOCK:
                reconcile_locked()
            if not REMOTE:
                continue
            if not SYNC["remote_loaded"]:
                try_remote_load()
            with LOCK:
                keys = list(DIRTY)
            for k in keys:
                push(k)
            backoff = 1
        except Exception as e:
            SYNC["last_error"] = str(e)[:200]
            print("Sync error:", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)


# ---------------------------------------------------------------- ความปลอดภัย
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    f"rcc|{ADMIN_PASSWORD}|{TOKEN_PIN}".encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=12 * 3600, MAX_CONTENT_LENGTH=16 * 1024,
)

FAILS = {}


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def is_locked(kind):
    rec = FAILS.get((kind, client_ip()))
    return bool(rec and rec[1] > time.time())


def register_fail(kind):
    rec = FAILS.setdefault((kind, client_ip()), [0, 0])
    rec[0] += 1
    if rec[0] >= 5:
        rec[1] = time.time() + 60
        rec[0] = 0


def same(a, b):
    return secrets.compare_digest(str(a).encode(), str(b).encode())


def admin_api(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return jsonify({"error": "หมดเวลาเข้าสู่ระบบ กรุณาล็อกอินใหม่"}), 401
        if request.method == "POST" and not request.is_json:
            return jsonify({"error": "bad request"}), 400
        return f(*a, **kw)
    return wrapper


def body_json():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def parse_seconds(v):
    if v is None or v == "":
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise ValueError("วินาทีต้องเป็นตัวเลข")
    if not MIN_SEC <= n <= MAX_SEC:
        raise ValueError(f"วินาทีต้องอยู่ระหว่าง {MIN_SEC}-{MAX_SEC}")
    return n


def parse_reason(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        raise ValueError("ข้อความเหตุผลห้ามว่าง")
    if len(v) > MAX_REASON:
        raise ValueError(f"ข้อความยาวเกิน {MAX_REASON} ตัวอักษร")
    return v


def parse_mode(v):
    if v is None or v in MODES or v == "cancel":
        return v
    raise ValueError("โหมดไม่ถูกต้อง")


def targets(pid):
    if pid == "ALL":
        return list(MAPS.values())
    if pid in PID_TO_NAME:
        return [pid]
    raise KeyError


# ---------------------------------------------------------------- Routes
@app.route("/", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        if is_locked("login"):
            return render_template_string(LOGIN_HTML, error="ลองผิดหลายครั้ง กรุณารอ 1 นาที")
        if same(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        register_fail("login")
        return render_template_string(LOGIN_HTML, error="รหัสผ่านไม่ถูกต้อง")
    if not session.get("ok"):
        return render_template_string(LOGIN_HTML, error=None)
    return render_template_string(HTML_TEMPLATE, maps=MAPS)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/overview")
@admin_api
def api_overview():
    r = jsonify(overview())
    r.headers["Cache-Control"] = "no-store"
    return r


def handle_write(pid, b, with_mode):
    try:
        mode = parse_mode(b.get("mode")) if with_mode else None
        reason = parse_reason(b.get("reason"))
        seconds = parse_seconds(b.get("seconds"))
        pids = targets(pid)
    except KeyError:
        return jsonify({"error": "ไม่พบแมพนี้"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with LOCK:
        for p in pids:
            update_state(p, mode, reason, seconds)
    return jsonify({"success": True})


@app.post("/api/save-config")
@admin_api
def api_save_config():
    b = body_json()
    return handle_write(b.get("place_id"), b, False)


@app.post("/api/update")
@admin_api
def api_update():
    b = body_json()
    if b.get("place_id") == "ALL":
        return jsonify({"error": "ใช้ /api/update-all"}), 400
    return handle_write(b.get("place_id"), b, True)


@app.post("/api/update-all")
@admin_api
def api_update_all():
    return handle_write("ALL", body_json(), True)


def check_pin():
    if is_locked("pin"):
        return jsonify({"error": "ลองผิดหลายครั้ง กรุณารอ 1 นาที"}), 429
    if not same(body_json().get("pin", ""), TOKEN_PIN):
        register_fail("pin")
        return jsonify({"error": "รหัส PIN ไม่ถูกต้อง"}), 403
    return None


@app.post("/api/token/reveal")
@admin_api
def api_token_reveal():
    err = check_pin()
    if err:
        return err
    with LOCK:
        return jsonify({"token": DATA["token"]})


@app.post("/api/token/new")
@admin_api
def api_token_new():
    err = check_pin()
    if err:
        return err
    with LOCK:
        DATA["token"] = secrets.token_urlsafe(32)
        DATA["token_updated_at"] = time.time()
        add_log("ALL", "token", "สร้าง Token ใหม่")
        DIRTY.add(TOKEN_KEY)
        save_local()
        WAKE.set()
        return jsonify({"token": DATA["token"]})


@app.get("/state/<place_id>")
def get_state(place_id):
    """สำหรับสคริปต์ Roblox (ฟอร์แมตเดิม + ฟิลด์เพิ่ม)"""
    with LOCK:
        expected = "Bearer " + DATA["token"]
    if not same(request.headers.get("Authorization", ""), expected):
        return jsonify({"error": "unauthorized"}), 401
    if place_id not in PID_TO_NAME:
        return jsonify({"error": "unknown place"}), 404
    r = jsonify(snapshot(place_id))
    r.headers["Cache-Control"] = "no-store"
    return r


# ---------------------------------------------------------------- หน้าเว็บ
LOGIN_HTML = """<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>เข้าสู่ระบบ</title>
<style>
body{font-family:'Segoe UI',Tahoma,sans-serif;background:#14161f;color:#d9dcec;display:grid;place-items:center;min-height:100vh;margin:0}
.box{background:#1d2030;padding:30px;border-radius:12px;width:min(350px,90vw);border:1px solid #2c3048}
h2{margin:0 0 18px;color:#fff;font-size:20px}
input,button{width:100%;padding:11px;border-radius:8px;box-sizing:border-box;font-size:15px}
input{background:#14161f;border:1px solid #3a3f5c;color:#fff;margin-bottom:12px}
button{background:#6f8cff;border:0;color:#fff;font-weight:700;cursor:pointer}
.err{color:#ff7a7a;font-size:14px;margin-bottom:10px}
</style></head><body><div class="box"><h2>เข้าสู่ระบบแอดมิน</h2>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="POST"><input type="password" name="password" placeholder="รหัสผ่านแอดมิน" autocomplete="current-password" required autofocus>
<button type="submit">เข้าสู่ระบบ</button></form></div></body></html>"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roblox Control Center</title>
<style>
:root{--bg:#14161f;--panel:#1d2030;--line:#2c3048;--ink:#d9dcec;--mute:#8b90ad;--acc:#6f8cff;--ok:#2fd39b;--warn:#ffb020;--bad:#ff5d6c}
*{box-sizing:border-box}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:16px;display:flex;justify-content:center}
.wrap{width:100%;max-width:760px;display:grid;gap:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
header{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:20px;margin:0;color:#fff}
h3{margin:0 0 12px;font-size:15px;color:#fff}
a.out{color:#fff;background:#3a3f5c;padding:6px 12px;border-radius:8px;text-decoration:none;font-size:13px}
.pill{font-size:12px;padding:4px 10px;border-radius:99px;border:1px solid var(--line);color:var(--mute)}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.warn{color:var(--warn);border-color:var(--warn)}.pill.bad{color:var(--bad);border-color:var(--bad)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--bg);border:1px solid var(--line);border-left:4px solid var(--mute);border-radius:10px;padding:14px}
.card.open{border-left-color:var(--ok)}.card.scheduled{border-left-color:var(--warn)}.card.closed{border-left-color:var(--bad)}
.card .top{display:flex;justify-content:space-between;align-items:center;font-weight:700;color:#fff}
.badge{font-size:12px;font-weight:600}
.open .badge{color:var(--ok)}.scheduled .badge{color:var(--warn)}.closed .badge{color:var(--bad)}
.big{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums;margin:8px 0 2px;color:#fff}
.sub{font-size:13px;color:var(--mute)}
.bar{height:6px;background:var(--line);border-radius:4px;margin:10px 0;overflow:hidden}
.bar i{display:block;height:100%;background:var(--warn);transition:width .9s linear}
dl{margin:10px 0 0;display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:12.5px}
dt{color:var(--mute)}dd{margin:0;word-break:break-word}
label{display:block;margin:0 0 5px;color:var(--acc);font-weight:600;font-size:13px}
.g{margin-bottom:12px}
select,input[type=text],input[type=password]{width:100%;padding:10px;background:var(--bg);border:1px solid #3a3f5c;color:#fff;border-radius:8px;font-size:14px}
.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1;min-width:120px}
button{padding:10px 14px;border:0;border-radius:8px;font-weight:700;color:#fff;cursor:pointer;font-size:14px}
button:disabled{opacity:.5;cursor:wait}
.b-close{background:#e5484d}.b-cancel{background:#e08a00}.b-open{background:#12a77c}.b-save{background:#3b82f6}.b-alt{background:#7c5cc4}.b-warn{background:#c9692a}
.chk{display:flex;gap:8px;align-items:center;margin:4px 0 12px}
.tok{font-family:monospace;color:var(--ok);word-break:break-all;margin:6px 0 10px;min-height:20px}
.log{max-height:230px;overflow:auto;font-size:13px;display:grid;gap:6px}
.log div{border-bottom:1px solid var(--line);padding-bottom:6px}.log span{color:var(--mute);font-size:12px}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#2a2f4a;color:#fff;padding:10px 18px;border-radius:10px;opacity:0;pointer-events:none;transition:opacity .2s;max-width:90vw;z-index:9}
#toast.show{opacity:1}#toast.err{background:#a3262f}
</style></head><body><div class="wrap">
<header class="panel"><h1>Roblox Control Center</h1>
<div class="row" style="flex:0;align-items:center"><span class="pill" id="conn">กำลังเชื่อมต่อ</span><span class="pill" id="syncPill">-</span><a class="out" href="/logout">ออกจากระบบ</a></div></header>

<section class="panel"><h3>สถานะแมพ</h3><div class="cards" id="cards"></div></section>

<section class="panel"><h3>สั่งงาน</h3>
<div class="g"><label>เลือกแมพ</label><select id="target"><option value="ALL">ทุกแมพ</option></select></div>
<div class="g"><label>เหตุผล / ข้อความที่แสดงตอนเตะ</label><input type="text" id="reason" maxlength="200"></div>
<div class="chk"><input type="checkbox" id="timerOn" checked><label for="timerOn" style="margin:0;color:var(--ink)">นับถอยหลังก่อนปิด</label></div>
<div class="g" id="secBox"><label>เวลานับถอยหลัง (วินาที, 1-86400)</label><input type="text" id="seconds" inputmode="numeric"></div>
<div class="row"><button class="b-save" data-act="save">บันทึกค่า</button><button class="b-close" data-act="close">เริ่มปิด</button>
<button class="b-cancel" data-act="cancel">ยกเลิกการปิด</button><button class="b-open" data-act="open">เปิดแมพ</button></div></section>

<section class="panel"><h3>API Token (สำหรับสคริปต์ Roblox)</h3>
<div class="tok" id="tok">••••••••••••••••••••••••</div>
<div class="row"><input type="password" id="pin" placeholder="PIN จัดการ Token" autocomplete="off">
<button class="b-alt" id="tokShow" style="flex:0">แสดง/ซ่อน</button><button class="b-warn" id="tokNew" style="flex:0">สร้างใหม่</button></div></section>

<section class="panel"><h3>ประวัติล่าสุด</h3><div class="log" id="log">ยังไม่มีรายการ</div></section>
</div><div id="toast"></div>

<script>
const MAPS = {{ maps|tojson }};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let st = null, offset = 0, touched = false, busy = false, lastOk = 0, lastPollAt = 0, tokenShown = false, tokenTimer = null, filled = null;

const nowS = () => Date.now() / 1000 + offset;
const pad = n => String(n).padStart(2, "0");
function fmt(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (d ? d + " วัน " : "") + pad(h) + ":" + pad(m) + ":" + pad(s);
}
const fdate = t => t ? new Date(t * 1000).toLocaleString("th-TH") : "-";

function toast(msg, isErr) {
  const t = $("toast"); t.textContent = msg; t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.className = "", 3200);
}
function setBusy(b) { busy = b; document.querySelectorAll("button").forEach(x => x.disabled = b); }

async function post(url, body, okMsg) {
  if (busy) return null;
  setBusy(true);
  try {
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if (r.status === 401) { location.href = "/"; return null; }
    if (!r.ok || d.error) { toast(d.error || "ผิดพลาด (" + r.status + ")", true); return null; }
    if (okMsg) toast(okMsg);
    touched = false;
    await poll();
    return d;
  } catch (e) { toast("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง", true); return null; }
  finally { setBusy(false); }
}

async function poll() {
  lastPollAt = Date.now();
  try {
    const r = await fetch("/api/overview", {cache: "no-store"});
    if (r.status === 401) { location.href = "/"; return; }
    if (!r.ok) throw new Error(r.status);
    st = await r.json();
    offset = st.serverNow - Date.now() / 1000;
    lastOk = Date.now();
    fillForm(); renderLog(); renderSync();
  } catch (e) {}
  renderConn(); renderCards();
}

function fillForm() {
  const t = $("target").value;
  const key = t + "|" + (t === "ALL" ? "" : st.places[t].updated_at);
  if (touched || filled === key) return;
  const p = t === "ALL" ? Object.values(st.places)[0] : st.places[t];
  $("reason").value = p.reason; $("seconds").value = p.seconds; filled = key;
}

function renderCards() {
  if (!st) return;
  const n = nowS();
  $("cards").innerHTML = Object.entries(st.places).map(([pid, p]) => {
    const el = n - p.changed_at;
    let label = {open: "เปิดให้บริการ", closed: "ปิดปรับปรุง", scheduled: "กำลังนับถอยหลัง"}[p.mode], big, sub, bar = "";
    if (p.mode === "scheduled") {
      const rem = Math.max(0, p.deadline - n), span = Math.max(1, p.deadline - p.changed_at);
      big = fmt(rem); sub = "เหลือเวลา · ผ่านไปแล้ว " + fmt(el);
      bar = '<div class="bar"><i style="width:' + (rem / span * 100).toFixed(1) + '%"></i></div>';
      if (rem <= 0 && Date.now() - lastPollAt > 1000) poll();
    } else { big = fmt(el); sub = (p.mode === "open" ? "เปิดมาแล้ว" : "ปิดมาแล้ว"); }
    return '<div class="card ' + p.mode + '"><div class="top"><span>' + esc(p.name) + '</span><span class="badge">' + label + '</span></div>' +
      '<div class="big">' + big + '</div><div class="sub">' + sub + '</div>' + bar +
      '<dl><dt>ข้อความ</dt><dd>' + esc(p.reason) + '</dd><dt>ตั้งเวลาไว้</dt><dd>' + p.seconds + ' วินาที</dd>' +
      '<dt>เปลี่ยนสถานะ</dt><dd>' + fdate(p.changed_at) + '</dd><dt>แก้ไขล่าสุด</dt><dd>' + fdate(p.updated_at) + '</dd>' +
      (p.mode === "scheduled" ? '<dt>จะปิดเวลา</dt><dd>' + fdate(p.deadline) + '</dd>' : '') +
      '<dt>Place ID</dt><dd>' + esc(pid) + '</dd></dl></div>';
  }).join("");
}

function renderConn() {
  const ok = Date.now() - lastOk < 8000, c = $("conn");
  c.textContent = ok ? "เชื่อมต่อแล้ว" : "ขาดการเชื่อมต่อ"; c.className = "pill " + (ok ? "ok" : "bad");
}
function renderSync() {
  const s = st.sync, p = $("syncPill");
  if (!s.remote_enabled) { p.textContent = "บันทึกในเครื่องเท่านั้น"; p.className = "pill warn"; return; }
  if (!s.remote_loaded) { p.textContent = "กำลังเชื่อม Supabase"; p.className = "pill warn"; return; }
  if (s.pending > 0 || s.last_error) {
    p.textContent = "รอซิงค์ " + s.pending + " รายการ"; p.className = "pill " + (s.last_error ? "bad" : "warn");
    p.title = s.last_error || ""; return;
  }
  p.textContent = "ซิงค์ Supabase แล้ว " + (s.last_ok ? new Date(s.last_ok * 1000).toLocaleTimeString("th-TH") : "");
  p.className = "pill ok"; p.title = s.extra_cols ? "" : "ตารางยังไม่มีคอลัมน์ changed_at/updated_at (ดู SQL)";
}
function renderLog() {
  $("log").innerHTML = st.log.length ? st.log.map(l =>
    '<div><span>' + fdate(l.t) + ' · ' + esc(l.place) + '</span><br>' + esc(l.detail) + '</div>').join("") : "ยังไม่มีรายการ";
}

function readForm() {
  const reason = $("reason").value.trim(), raw = $("seconds").value.trim();
  const sec = Number(raw);
  if (!reason) { toast("กรุณากรอกข้อความเหตุผล", true); return null; }
  if (!Number.isInteger(sec) || sec < 1 || sec > 86400) { toast("วินาทีต้องเป็นจำนวนเต็ม 1-86400", true); return null; }
  return {reason, seconds: sec};
}

async function act(kind) {
  const f = readForm(); if (!f) return;
  const t = $("target").value, all = t === "ALL";
  if (all && !confirm("ดำเนินการกับทุกแมพพร้อมกัน ยืนยันหรือไม่?")) return;
  const body = {...f};
  if (kind === "close") body.mode = $("timerOn").checked ? "scheduled" : "closed";
  if (kind === "open") body.mode = "open";
  if (kind === "cancel") body.mode = "cancel";
  let url;
  if (kind === "save") url = "/api/save-config";
  else url = all ? "/api/update-all" : "/api/update";
  body.place_id = t;
  const msg = {save: "บันทึกค่าแล้ว", close: "ส่งคำสั่งปิดแล้ว", open: "เปิดแมพแล้ว", cancel: "ยกเลิกการปิดแล้ว"}[kind];
  await post(url, body, msg);
}

function hideToken() { tokenShown = false; $("tok").textContent = "••••••••••••••••••••••••"; clearTimeout(tokenTimer); }
function showToken(t) { tokenShown = true; $("tok").textContent = t; clearTimeout(tokenTimer); tokenTimer = setTimeout(hideToken, 30000); }

document.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(b.dataset.act));
Object.entries(MAPS).forEach(([n, id]) => $("target").add(new Option(n, id)));
$("target").onchange = () => { touched = false; filled = null; if (st) fillForm(); };
["reason", "seconds"].forEach(i => $(i).oninput = () => touched = true);
$("timerOn").onchange = () => $("secBox").style.display = $("timerOn").checked ? "block" : "none";
$("tokShow").onclick = async () => {
  if (tokenShown) return hideToken();
  const d = await post("/api/token/reveal", {pin: $("pin").value}); if (d) showToken(d.token);
};
$("tokNew").onclick = async () => {
  if (!confirm("สร้าง Token ใหม่? สคริปต์ Roblox เดิมจะใช้ไม่ได้ทันทีจนกว่าจะเปลี่ยน Token")) return;
  const d = await post("/api/token/new", {pin: $("pin").value}, "สร้าง Token ใหม่แล้ว"); if (d) showToken(d.token);
};

poll(); setInterval(poll, 2500); setInterval(() => { renderCards(); renderConn(); }, 1000);
</script></body></html>"""


# ---------------------------------------------------------------- เริ่มระบบ
def init():
    for pid in MAPS.values():
        DATA["places"].setdefault(pid, new_state())
    load_local()
    if REMOTE:
        try:
            try_remote_load()
        except Exception as e:
            SYNC["last_error"] = str(e)[:200]
            print("Initial Supabase load failed (จะลองใหม่เบื้องหลัง):", e)
    else:
        for key in [TOKEN_KEY, *MAPS.values()]:
            DIRTY.add(key)
    save_local()
    threading.Thread(target=sync_worker, daemon=True, name="sync").start()


init()

if __name__ == "__main__":
    print(f"Control Center พร้อมที่ port {PORT} | Supabase: {'เปิด' if REMOTE else 'ปิด (ใช้ไฟล์ในเครื่อง)'}")
    serve(app, host=HOST, port=PORT, threads=8)

# ---------------------------------------------------------------- SQL (รันครั้งเดียวใน Supabase SQL Editor)
# alter table control_state
#   add column if not exists changed_at double precision,
#   add column if not exists updated_at double precision;
# และ place_id ต้องเป็น primary key / unique
