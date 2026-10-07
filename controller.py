import copy
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import wraps

from flask import Flask, jsonify, redirect, render_template_string, request, session
from waitress import serve


# ============================================================
# Roblox Control Center - Stable Edition
# ============================================================
# Required Render Environment Variables:
#   ADMIN_PASSWORD
#   TOKEN_PIN
#   SECRET_KEY
#   SUPABASE_URL
#   SUPABASE_KEY
#
# Existing Supabase table:
#   control_state
#
# Required columns:
#   place_id   text   (PRIMARY KEY or UNIQUE is REQUIRED for upsert)
#   mode       text
#   reason     text
#   deadline   double precision / numeric / nullable
#   seconds    integer / nullable
#
# The code intentionally keeps the existing API:
#   GET /state/<place_id>       Roblox API
#   GET /api/state/<place_id>   Dashboard API
#   POST /api/update
#   POST /api/update-all
#   POST /api/save-config
# ============================================================


# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOG = logging.getLogger("roblox-control-center")


# -------------------- Environment --------------------
def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )
    return value


ADMIN_PASSWORD = env_required("6155045")
TOKEN_PIN = env_required("991675788")
SUPABASE_URL = env_required("https://roiuyoflkmftbxtdeuax.supabase.co").rstrip("/")
SUPABASE_KEY = env_required("sb_secret_EvnaRhy6JMuZm0W_TkAZIg_MnepRC9m")

# A stable SECRET_KEY is important for Flask sessions.
# Generate one locally with:
# python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.environ.get("SECRET_KEY", "846a31776edb467c84a160fd8f9b70d0437850c112bc8bc74a17aaba3b52db30").strip()
if not SECRET_KEY:
    raise RuntimeError(
        "Missing SECRET_KEY. Add a long random value to Render Environment."
    )


# -------------------- Server --------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8888"))

# Never expose secrets in logs.
DEFAULT_REASON = "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง"

MAPS = {
    "Place 1": "120651982896178",
    "Place 2": "77210125175879",
}

PLACE_IDS = tuple(MAPS.values())
SYSTEM_TOKEN_ID = "__SYS_TOKEN__"

# LOCK       : protects the in-memory DATA only (never held during network I/O)
# WRITE_LOCK : serialises database writes so slow Supabase calls
#              never block readers (e.g. Roblox polling)
LOCK = threading.RLock()
WRITE_LOCK = threading.Lock()
DB_AVAILABLE = False
LAST_DB_SYNC = None

# Database load state. If Supabase is unreachable at boot, loading is
# retried automatically (see ensure_db_loaded) instead of staying broken.
INITIALIZED = False
INIT_LOCK = threading.Lock()
LAST_INIT_ATTEMPT = 0.0


# -------------------- Flask --------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    MAX_CONTENT_LENGTH=1024 * 1024,
)


# -------------------- In-memory state --------------------
def default_state():
    return {
        "mode": "open",
        "reason": DEFAULT_REASON,
        "deadline": None,
        "seconds": 60,
    }


DATA = {
    "token": None,
    "places": {pid: default_state() for pid in PLACE_IDS},
}


# ============================================================
# Supabase REST
# ============================================================

def supabase_headers(prefer=None):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_request(method, path, payload=None, query=None, timeout=8, prefer=None):
    """
    Small REST client using urllib only.
    Raises RuntimeError on HTTP/network/JSON errors.
    """
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"

    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)

    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers=supabase_headers(prefer),
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)

        raise RuntimeError(
            f"Supabase HTTP {exc.code}: {detail[:800]}"
        ) from exc

    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Supabase connection failed: {exc}"
        ) from exc


def load_from_supabase():
    """
    Loads the complete control_state table.
    Returns:
        {"token": str, "places": {...}, "missing": [place_id, ...]}
    """
    global DB_AVAILABLE, LAST_DB_SYNC

    rows = supabase_request(
        "GET",
        "control_state",
        query={
            "select": "place_id,mode,reason,deadline,seconds",
        },
        timeout=8,
    )

    if not isinstance(rows, list):
        raise RuntimeError("Supabase returned an unexpected response.")

    result = {
        "token": None,
        "places": {},
    }

    found = set()

    for row in rows:
        place_id = str(row.get("place_id", ""))

        if place_id == SYSTEM_TOKEN_ID:
            token = row.get("reason")
            if token:
                result["token"] = str(token)
            continue

        if place_id not in PLACE_IDS:
            continue

        found.add(place_id)

        mode = row.get("mode") or "open"
        if mode not in {"open", "scheduled", "closed"}:
            mode = "open"

        reason = row.get("reason") or DEFAULT_REASON

        raw_seconds = row.get("seconds", 60)
        try:
            seconds = int(raw_seconds)
        except (TypeError, ValueError):
            seconds = 60

        seconds = max(1, min(seconds, 86400))

        deadline = row.get("deadline")
        try:
            deadline = float(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline = None

        result["places"][place_id] = {
            "mode": mode,
            "reason": str(reason)[:500],
            "deadline": deadline,
            "seconds": seconds,
        }

    # Rows that really do not exist in the database yet.
    result["missing"] = [pid for pid in PLACE_IDS if pid not in found]

    for pid in PLACE_IDS:
        result["places"].setdefault(pid, default_state())

    DB_AVAILABLE = True
    LAST_DB_SYNC = time.time()
    return result


def save_to_supabase(place_id, state_data):
    """
    Upserts one row and verifies that Supabase accepted it.
    """
    if place_id == SYSTEM_TOKEN_ID:
        payload = {
            "place_id": SYSTEM_TOKEN_ID,
            "mode": "token",
            "reason": str(state_data["token"]),
            "deadline": None,
            "seconds": 0,
        }
    else:
        payload = {
            "place_id": place_id,
            "mode": state_data.get("mode", "open"),
            "reason": state_data.get("reason", DEFAULT_REASON),
            "deadline": state_data.get("deadline"),
            "seconds": int(state_data.get("seconds", 60)),
        }

    # PostgREST upsert. place_id must be PRIMARY KEY/UNIQUE, and the
    # "Prefer: resolution=merge-duplicates" header is REQUIRED, otherwise
    # existing rows are rejected with HTTP 409 (duplicate key).
    result = supabase_request(
        "POST",
        "control_state",
        payload=payload,
        query={"on_conflict": "place_id"},
        timeout=8,
        prefer="resolution=merge-duplicates,return=minimal",
    )

    return result


def save_with_retry(place_id, state_data, retries=3):
    global DB_AVAILABLE, LAST_DB_SYNC

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            save_to_supabase(place_id, state_data)
            DB_AVAILABLE = True
            LAST_DB_SYNC = time.time()
            return True, None
        except Exception as exc:
            last_error = str(exc)
            LOG.warning(
                "Supabase save failed (%s/%s) for %s: %s",
                attempt,
                retries,
                place_id,
                last_error,
            )
            if attempt < retries:
                time.sleep(0.25 * attempt)

    DB_AVAILABLE = False
    return False, last_error


def initialize_database():
    """
    Load existing data. If individual map rows are missing, create them.
    If the token row is missing, generate one and persist it.

    Returns True only when the database state was fully loaded.
    """
    global DB_AVAILABLE, INITIALIZED

    try:
        loaded = load_from_supabase()
    except Exception as exc:
        DB_AVAILABLE = False
        LOG.error("Supabase load failed: %s", exc)
        LOG.error(
            "The service will use safe defaults and retry loading "
            "automatically until Supabase is reachable."
        )
        return False

    with LOCK:
        DATA["places"].update(loaded["places"])
        if loaded.get("token"):
            DATA["token"] = loaded["token"]

    if not DATA.get("token"):
        new_token = secrets.token_urlsafe(32)
        ok, err = save_with_retry(
            SYSTEM_TOKEN_ID,
            {"token": new_token},
        )
        if not ok:
            # Never keep an unsaved token in memory: after a restart it
            # would silently change. Try again later.
            LOG.error("Could not save initial API token: %s", err)
            return False

        with LOCK:
            DATA["token"] = new_token

    for pid in loaded.get("missing", []):
        state = default_state()
        ok, err = save_with_retry(pid, state)
        if ok:
            with LOCK:
                DATA["places"][pid] = state
        else:
            LOG.error(
                "Could not initialize %s: %s",
                pid,
                err,
            )

    INITIALIZED = True
    LOG.info("Database initialization complete.")
    return True


def ensure_db_loaded():
    """
    If the first load failed (e.g. Supabase was down at boot), retry at
    most every 5 seconds. Only one thread attempts it; the others
    continue immediately without waiting.
    """
    global LAST_INIT_ATTEMPT

    if INITIALIZED:
        return

    if time.time() - LAST_INIT_ATTEMPT < 5:
        return

    if not INIT_LOCK.acquire(blocking=False):
        return

    try:
        if INITIALIZED:
            return
        LAST_INIT_ATTEMPT = time.time()
        initialize_database()
    finally:
        INIT_LOCK.release()


# ============================================================
# State handling
# ============================================================

def normalize_seconds(value, default=60):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default

    return max(1, min(number, 86400))


def normalize_reason(value):
    if value is None:
        return None

    reason = str(value).strip()
    if not reason:
        return DEFAULT_REASON

    return reason[:500]


def reconcile_state_locked(place_id):
    """
    If a scheduled deadline has passed, transition to closed.

    IMPORTANT:
    We intentionally keep deadline equal to the actual closing timestamp.
    This lets the dashboard show "ปิดมาแล้ว X" after a restart.
    Roblox only needs mode=closed, so retaining deadline is harmless.
    """
    state = DATA["places"][place_id]

    if (
        state["mode"] == "scheduled"
        and state["deadline"] is not None
        and time.time() >= float(state["deadline"])
    ):
        state["mode"] = "closed"
        return True

    return False


def persist_transition(place_id):
    """
    Persist a scheduled->closed transition in the background.
    If an admin update is currently running, it already saves the
    latest state, so we simply skip.
    """
    if not WRITE_LOCK.acquire(blocking=False):
        return

    try:
        with LOCK:
            current = copy.deepcopy(DATA["places"][place_id])

        ok, err = save_with_retry(place_id, current, retries=2)
        if not ok:
            LOG.error(
                "Could not persist scheduled->closed transition for %s: %s",
                place_id,
                err,
            )
    finally:
        WRITE_LOCK.release()


def snapshot(place_id):
    ensure_db_loaded()

    with LOCK:
        changed = reconcile_state_locked(place_id)
        state = copy.deepcopy(DATA["places"][place_id])

    # Roblox receives the correct closed state immediately; persistence
    # happens in the background and never blocks the request.
    if changed:
        threading.Thread(
            target=persist_transition,
            args=(place_id,),
            daemon=True,
        ).start()

    now = time.time()

    state["serverNow"] = now
    state["dbAvailable"] = DB_AVAILABLE
    state["lastDbSync"] = LAST_DB_SYNC

    # Client-friendly derived values.
    if state["mode"] == "scheduled" and state["deadline"] is not None:
        state["startedAt"] = float(state["deadline"]) - state["seconds"]
        state["remaining"] = max(
            0,
            float(state["deadline"]) - now,
        )
        state["elapsed"] = min(
            state["seconds"],
            max(0, now - state["startedAt"]),
        )
    elif state["mode"] == "closed" and state["deadline"] is not None:
        state["closedAt"] = float(state["deadline"])
        state["closedElapsed"] = max(
            0,
            now - float(state["deadline"]),
        )
        state["remaining"] = 0
        state["elapsed"] = state["seconds"]
    else:
        state["remaining"] = 0
        state["elapsed"] = 0

    return state


def update_state(place_id, mode=None, reason=None, seconds=None):
    """
    Build the new state, save it to Supabase (without holding LOCK),
    and only then publish it to memory.

    Memory never contains an unsaved state, so no rollback is needed and
    slow database calls never block readers.
    """
    if place_id not in PLACE_IDS:
        raise ValueError("unknown place")

    if mode is not None and mode not in {"open", "scheduled", "closed"}:
        raise ValueError("invalid mode")

    with WRITE_LOCK:
        with LOCK:
            reconcile_state_locked(place_id)
            new_state = copy.deepcopy(DATA["places"][place_id])

        if reason is not None:
            normalized_reason = normalize_reason(reason)
            if normalized_reason is not None:
                new_state["reason"] = normalized_reason

        if seconds is not None:
            new_state["seconds"] = normalize_seconds(seconds)

        if mode is not None:
            new_state["mode"] = mode

            if mode == "scheduled":
                duration = normalize_seconds(
                    seconds if seconds is not None else new_state["seconds"]
                )
                new_state["seconds"] = duration
                new_state["deadline"] = time.time() + duration

            elif mode == "closed":
                # Keep a timestamp so the dashboard can show how long
                # the server has been closed.
                new_state["deadline"] = time.time()
                new_state["seconds"] = max(
                    1,
                    int(new_state.get("seconds", 60)),
                )

            elif mode == "open":
                new_state["deadline"] = None

        # Persist first. Do not tell the browser "success" if DB failed.
        ok, error = save_with_retry(place_id, new_state)

        if not ok:
            raise RuntimeError(
                f"บันทึกฐานข้อมูลไม่สำเร็จ: {error}"
            )

        with LOCK:
            DATA["places"][place_id] = new_state

    return snapshot(place_id)


# ============================================================
# Authentication
# ============================================================

def safe_equal(a, b):
    """
    Constant-time comparison on bytes.
    secrets.compare_digest on str raises TypeError for non-ASCII text,
    which would turn odd input into a 500 error.
    """
    return secrets.compare_digest(
        str(a or "").encode("utf-8"),
        str(b or "").encode("utf-8"),
    )


def logged_in():
    return session.get("admin_authenticated") is True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not logged_in():
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/")
        return view(*args, **kwargs)

    return wrapped


def verify_token_pin(value):
    return safe_equal(value, TOKEN_PIN)


# ============================================================
# HTML - Login
# ============================================================

LOGIN_HTML = r"""
<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login · Roblox Control Center</title>
<style>
*{box-sizing:border-box}
body{
    margin:0;
    min-height:100vh;
    display:grid;
    place-items:center;
    padding:20px;
    color:#eef2ff;
    background:
      radial-gradient(circle at 20% 10%,rgba(99,102,241,.20),transparent 30%),
      radial-gradient(circle at 80% 90%,rgba(14,165,233,.14),transparent 30%),
      #070a12;
    font-family:Inter,Segoe UI,Arial,sans-serif;
}
.card{
    width:min(420px,100%);
    padding:32px;
    border:1px solid #252b3d;
    border-radius:24px;
    background:rgba(15,19,31,.92);
    box-shadow:0 24px 80px rgba(0,0,0,.45);
}
.logo{
    width:58px;height:58px;
    display:grid;place-items:center;
    border-radius:17px;
    background:linear-gradient(135deg,#6366f1,#06b6d4);
    font-size:27px;
    margin-bottom:20px;
}
h1{margin:0 0 8px;font-size:25px}
p{color:#8e99ad;margin:0 0 24px}
label{display:block;font-size:13px;color:#aeb8ca;margin-bottom:8px}
input{
    width:100%;height:48px;padding:0 14px;
    color:#fff;background:#0b0f19;
    border:1px solid #293147;border-radius:12px;outline:none;
}
input:focus{border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.15)}
button{
    width:100%;height:48px;margin-top:14px;
    border:0;border-radius:12px;
    color:#fff;font-weight:700;cursor:pointer;
    background:linear-gradient(135deg,#6366f1,#4f46e5);
}
.error{
    padding:11px 13px;margin-bottom:15px;
    border-radius:10px;background:#3a1218;color:#ffb4be;
    border:1px solid #69202c;font-size:13px
}
.small{margin-top:18px;text-align:center;font-size:12px;color:#657086}
</style>
</head>
<body>
<div class="card">
    <div class="logo">🛡️</div>
    <h1>Roblox Control Center</h1>
    <p>เข้าสู่ระบบผู้ดูแลระบบ</p>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="post">
        <label>Admin Password</label>
        <input type="password" name="password" autocomplete="current-password"
               placeholder="กรอกรหัสผ่าน" required autofocus>
        <button type="submit">เข้าสู่ระบบ →</button>
    </form>
    <div class="small">Secure administrator dashboard</div>
</div>
</body>
</html>
"""


# ============================================================
# HTML - Dashboard
# ============================================================

DASHBOARD_HTML = r"""
<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Roblox Control Center</title>
<style>
:root{
    --bg:#070a12;
    --panel:#0f1420;
    --panel2:#0b101a;
    --border:#242c3e;
    --text:#eef2ff;
    --muted:#8994a8;
    --blue:#6366f1;
    --cyan:#06b6d4;
    --green:#22c55e;
    --yellow:#f59e0b;
    --red:#ef4444;
}
*{box-sizing:border-box}
body{
    margin:0;
    color:var(--text);
    background:
      radial-gradient(circle at 10% 0%,rgba(99,102,241,.13),transparent 28%),
      radial-gradient(circle at 100% 100%,rgba(6,182,212,.08),transparent 30%),
      var(--bg);
    font-family:Inter,Segoe UI,Arial,sans-serif;
}
button,input,select{font:inherit}
.app{width:min(1180px,100%);margin:auto;padding:22px}
.header{
    display:flex;justify-content:space-between;gap:15px;align-items:center;
    padding:20px 22px;margin-bottom:18px;
    border:1px solid var(--border);border-radius:20px;
    background:rgba(15,20,32,.88);
    box-shadow:0 20px 60px rgba(0,0,0,.22);
}
.brand{display:flex;align-items:center;gap:13px}
.logo{
    width:48px;height:48px;border-radius:15px;
    display:grid;place-items:center;font-size:23px;
    background:linear-gradient(135deg,var(--blue),var(--cyan));
}
h1{font-size:19px;margin:0}
.sub{font-size:12px;color:var(--muted);margin-top:3px}
.logout{
    text-decoration:none;color:#ffc1c1;
    border:1px solid #51222a;background:#211016;
    padding:9px 13px;border-radius:10px;font-size:12px
}
.grid{display:grid;grid-template-columns:1.35fr .85fr;gap:18px}
.panel{
    background:rgba(15,20,32,.90);
    border:1px solid var(--border);
    border-radius:20px;padding:20px;
}
.panel h2{font-size:15px;margin:0 0 17px}
.field{margin-bottom:15px}
label{
    display:block;margin-bottom:7px;
    color:#aab4c7;font-size:12px;font-weight:700
}
input[type=text],input[type=number],input[type=password],select{
    width:100%;height:44px;padding:0 12px;
    color:#f8fafc;background:#090d16;
    border:1px solid #273047;border-radius:10px;outline:none
}
input:focus,select:focus{
    border-color:var(--blue);
    box-shadow:0 0 0 3px rgba(99,102,241,.12)
}
.row{display:grid;grid-template-columns:1fr 170px;gap:12px}
.check{
    display:flex;align-items:center;gap:9px;
    padding:11px 12px;border:1px solid var(--border);
    border-radius:10px;background:#0b1019;margin-bottom:14px
}
.check input{width:17px;height:17px}
.check label{margin:0;color:#dce3f1}
.buttons{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
.btn{
    min-height:44px;padding:10px 12px;
    border:0;border-radius:10px;color:#fff;
    font-weight:750;cursor:pointer;transition:.15s;
}
.btn:hover{transform:translateY(-1px);filter:brightness(1.08)}
.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.close{background:#b91c1c}
.cancel{background:#b45309}
.open{background:#15803d}
.all{grid-column:1/-1}
.blue{background:#4338ca}
.gray{background:#1f2937}
.status{
    position:relative;overflow:hidden;
    border:1px solid var(--border);
    border-radius:16px;padding:17px;
    background:#090e18;margin-bottom:12px
}
.status::before{
    content:"";position:absolute;left:0;top:0;bottom:0;width:4px;
    background:var(--green)
}
.status.scheduled::before{background:var(--yellow)}
.status.closed::before{background:var(--red)}
.status-head{display:flex;justify-content:space-between;gap:10px}
.badge{
    display:inline-flex;align-items:center;gap:6px;
    padding:5px 8px;border-radius:999px;
    font-size:11px;font-weight:800;background:#102719;color:#86efac
}
.badge.scheduled{background:#2b210c;color:#fcd34d}
.badge.closed{background:#2a1115;color:#fca5a5}
.big{font-size:27px;font-weight:850;margin:12px 0 3px}
.timer{
    font-variant-numeric:tabular-nums;
    font-size:35px;font-weight:900;letter-spacing:1px
}
.progress{
    height:7px;background:#1c2535;border-radius:99px;overflow:hidden;margin:12px 0
}
.progress > div{height:100%;width:0;background:linear-gradient(90deg,var(--blue),var(--cyan))}
.details{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px}
.detail{
    padding:9px 10px;border-radius:9px;background:#0d1320;
    border:1px solid #1d2638
}
.detail b{display:block;color:#68758b;font-size:10px;margin-bottom:3px}
.detail span{font-size:12px;color:#dbe3f0;word-break:break-word}
.reason{
    margin-top:10px;padding:10px;border-radius:9px;
    background:#0d1320;color:#bac5d7;font-size:12px
}
.token{
    margin-top:18px;padding:15px;
    border:1px solid var(--border);border-radius:15px;background:#090e18
}
.token-value{
    margin-top:8px;padding:10px;
    background:#060912;border-radius:9px;
    color:#67e8f9;font-family:ui-monospace,Consolas,monospace;
    font-size:12px;word-break:break-all
}
.token-actions{display:grid;grid-template-columns:1fr auto auto;gap:8px;margin-top:9px}
.notice{
    position:fixed;right:18px;bottom:18px;
    max-width:390px;padding:13px 15px;
    border:1px solid #2b3850;border-radius:12px;
    background:#111827;color:#e5e7eb;
    box-shadow:0 15px 50px rgba(0,0,0,.4);
    display:none;z-index:100
}
.notice.show{display:block}
.notice.ok{border-color:#185b35}
.notice.bad{border-color:#6b2630}
.db{
    font-size:11px;padding:5px 8px;border-radius:999px;
    background:#102719;color:#86efac
}
.db.off{background:#2a1115;color:#fca5a5}
@media(max-width:850px){
    .grid{grid-template-columns:1fr}
}
@media(max-width:600px){
    .app{padding:12px}
    .header{padding:15px}
    .buttons{grid-template-columns:1fr}
    .row{grid-template-columns:1fr}
    .details{grid-template-columns:1fr}
    .token-actions{grid-template-columns:1fr}
}
</style>
</head>

<body>
<div class="app">

<header class="header">
    <div class="brand">
        <div class="logo">🛡️</div>
        <div>
            <h1>Roblox Control Center</h1>
            <div class="sub">ระบบควบคุมสถานะเซิร์ฟเวอร์แบบ Real-time</div>
        </div>
    </div>
    <div style="display:flex;align-items:center;gap:9px">
        <span id="dbBadge" class="db">● DATABASE OK</span>
        <a class="logout" href="/logout">ออกจากระบบ</a>
    </div>
</header>

<div class="grid">

<section class="panel">
    <h2>⚙️ การควบคุม</h2>

    <div class="field">
        <label>เลือกแมพ</label>
        <select id="mapSelect" onchange="loadSelected()">
            <option value="ALL">🌐 จัดการทุกแมพ</option>
            {% for name, pid in maps.items() %}
            <option value="{{ pid }}">{{ name }} · {{ pid }}</option>
            {% endfor %}
        </select>
    </div>

    <div class="field">
        <label>เหตุผล / ข้อความแจ้งเตือน</label>
        <input id="reasonInput" type="text"
               value="{{ default_reason }}"
               maxlength="500">
    </div>

    <div class="row">
        <div class="field">
            <label>เวลา Countdown (วินาที)</label>
            <input id="secondsInput" type="number"
                   min="1" max="86400" value="60">
        </div>
        <div class="field">
            <label>สถานะ Timer</label>
            <div class="check">
                <input id="timerEnabled" type="checkbox" checked>
                <label for="timerEnabled">นับถอยหลังก่อนปิด</label>
            </div>
        </div>
    </div>

    <div class="buttons">
        <button class="btn close" onclick="actionSelected('scheduled')">
            🛑 เริ่มปิด
        </button>
        <button class="btn cancel" onclick="actionSelected('open')">
            ↩ ยกเลิก / เปิด
        </button>
        <button class="btn open" onclick="actionSelected('open')">
            🟢 เปิดแมพ
        </button>

        <button class="btn close all" onclick="actionAll('closed')">
            🚨 ปิดทุกแมพทันที
        </button>
        <button class="btn cancel all" onclick="actionAll('open')">
            ↩️ เปิด / ยกเลิกการปิดทุกแมพ
        </button>
        <button class="btn blue all" onclick="actionAll('scheduled')">
            ⏳ Countdown ทุกแมพ
        </button>
    </div>

    <div style="margin-top:10px">
        <button class="btn gray" style="width:100%" onclick="saveConfig()">
            💾 บันทึกข้อความ + เวลา โดยไม่เปลี่ยนสถานะ
        </button>
    </div>
</section>


<section class="panel">
    <h2>📡 สถานะระบบ</h2>
    <div id="statusList">
        <div class="status">กำลังโหลด...</div>
    </div>

    <div class="token">
        <div style="font-weight:800;font-size:13px">🔑 Roblox API Token</div>
        <div class="sub" style="margin-top:4px">
            Token จะไม่ถูกส่งลงหน้าเว็บจนกว่าจะยืนยัน PIN
        </div>

        <div id="tokenDisplay" class="token-value">
            ••••••••••••••••••••••••••••••••
        </div>

        <div class="token-actions">
            <input id="pinInput" type="password" placeholder="Token PIN"
                   autocomplete="off">
            <button class="btn blue" onclick="revealToken()">👁️ ดู Token</button>
            <button class="btn close" onclick="newToken()">🔄 สร้างใหม่</button>
        </div>
    </div>
</section>

</div>
</div>

<div id="notice" class="notice"></div>

<script>
let selectedState = null;
let tokenVisible = false;
let actualToken = "";
let serverOffsetMs = 0;
let busy = false;

const $ = id => document.getElementById(id);

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&":"&amp;",
        "<":"&lt;",
        ">":"&gt;",
        '"':"&quot;",
        "'":"&#039;"
    }[ch]));
}

function notify(message, ok=true) {
    const box = $("notice");
    box.textContent = message;
    box.className = "notice show " + (ok ? "ok" : "bad");
    clearTimeout(notify.timer);
    notify.timer = setTimeout(() => {
        box.className = "notice";
    }, 4200);
}

function setBusy(value) {
    busy = value;
    document.querySelectorAll("button").forEach(btn => {
        btn.disabled = value;
    });
}

function fmtDuration(totalSeconds) {
    totalSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const d = Math.floor(totalSeconds / 86400);
    totalSeconds %= 86400;
    const h = Math.floor(totalSeconds / 3600);
    totalSeconds %= 3600;
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;

    if (d > 0) {
        return `${d}d ${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
    }
    return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function fmtClock(ts) {
    if (!ts) return "-";
    return new Date(Number(ts) * 1000).toLocaleTimeString("th-TH", {
        hour:"2-digit",
        minute:"2-digit",
        second:"2-digit"
    });
}

function currentServerTime() {
    return (Date.now() + serverOffsetMs) / 1000;
}

function modeInfo(mode) {
    if (mode === "scheduled") {
        return ["🟠 กำลังนับถอยหลัง", "scheduled"];
    }
    if (mode === "closed") {
        return ["🔴 ปิดเซิร์ฟเวอร์", "closed"];
    }
    return ["🟢 เปิดให้บริการ", "open"];
}

function renderStatus(states) {
    const list = $("statusList");
    if (!states || !states.length) {
        list.innerHTML = `<div class="status">ไม่พบข้อมูลสถานะ</div>`;
        return;
    }

    list.innerHTML = states.map(item => {
        const [label, cls] = modeInfo(item.mode);
        const now = currentServerTime();

        let timer = "เปิดอยู่";
        let progress = 0;
        let extra = "";

        if (item.mode === "scheduled" && item.deadline) {
            const remaining = Math.max(0, Number(item.deadline) - now);
            const total = Math.max(1, Number(item.seconds || 60));
            const elapsed = Math.min(total, Math.max(0, total - remaining));
            progress = Math.min(100, (elapsed / total) * 100);
            timer = fmtDuration(remaining);
            extra = `
                <div class="detail">
                    <b>ผ่านไปแล้ว</b>
                    <span>${fmtDuration(elapsed)}</span>
                </div>
                <div class="detail">
                    <b>เหลืออีก</b>
                    <span>${fmtDuration(remaining)}</span>
                </div>
                <div class="detail">
                    <b>เริ่มนับ</b>
                    <span>${fmtClock(Number(item.deadline) - total)}</span>
                </div>
                <div class="detail">
                    <b>กำหนดปิด</b>
                    <span>${fmtClock(item.deadline)}</span>
                </div>`;
        } else if (item.mode === "closed" && item.deadline) {
            const elapsed = Math.max(0, now - Number(item.deadline));
            timer = fmtDuration(elapsed);
            extra = `
                <div class="detail">
                    <b>ปิดเมื่อ</b>
                    <span>${fmtClock(item.deadline)}</span>
                </div>
                <div class="detail">
                    <b>ปิดมาแล้ว</b>
                    <span>${fmtDuration(elapsed)}</span>
                </div>`;
        } else {
            extra = `
                <div class="detail">
                    <b>Server Time</b>
                    <span>${fmtClock(now)}</span>
                </div>
                <div class="detail">
                    <b>Countdown</b>
                    <span>ไม่ได้ทำงาน</span>
                </div>`;
        }

        const reason = escapeHtml(item.reason || "-");
        const name = escapeHtml(item.name || item.place_id);

        return `
        <div class="status ${cls}" data-place="${escapeHtml(item.place_id)}">
            <div class="status-head">
                <div>
                    <div style="font-size:12px;color:#7f8ba0">${name}</div>
                    <div class="big">${timer}</div>
                </div>
                <span class="badge ${cls}">${label}</span>
            </div>

            ${item.mode === "scheduled"
                ? `<div class="progress"><div style="width:${progress}%"></div></div>`
                : ""}

            <div class="details">
                <div class="detail">
                    <b>Place ID</b>
                    <span>${escapeHtml(item.place_id)}</span>
                </div>
                <div class="detail">
                    <b>สถานะ Database</b>
                    <span>${item.dbAvailable ? "🟢 เชื่อมต่อแล้ว" : "🔴 Offline"}</span>
                </div>
                ${extra}
            </div>

            <div class="reason">
                <b style="color:#7f8ba0">เหตุผล:</b>
                ${reason}
            </div>
        </div>`;
    }).join("");

    const dbOk = states.every(s => s.dbAvailable !== false);
    $("dbBadge").textContent = dbOk ? "● DATABASE OK" : "● DATABASE OFFLINE";
    $("dbBadge").className = dbOk ? "db" : "db off";
}

function applyFormState(item) {
    if (!item) return;
    $("reasonInput").value = item.reason || "";
    $("secondsInput").value = item.seconds || 60;
    $("timerEnabled").checked = item.mode === "scheduled" || item.mode === "open";
}

async function api(url, options={}) {
    const response = await fetch(url, {
        cache:"no-store",
        ...options,
        headers:{
            "Content-Type":"application/json",
            ...(options.headers || {})
        }
    });

    let data = null;
    try {
        data = await response.json();
    } catch (_) {}

    if (!response.ok) {
        const message = data?.error || `HTTP ${response.status}`;
        throw new Error(message);
    }
    return data;
}

// force=true is used right after an action, while busy is still true.
async function loadSelected(force=false) {
    if (busy && !force) return;

    const pid = $("mapSelect").value;

    try {
        if (pid === "ALL") {
            const data = await api("/api/states");
            if (data.serverNow) {
                serverOffsetMs = Number(data.serverNow) * 1000 - Date.now();
            }
            renderStatus(data.states);

            if (data.states?.length) {
                applyFormState(data.states[0]);
            }
            return;
        }

        const data = await api("/api/state/" + encodeURIComponent(pid));
        selectedState = data;

        if (data.serverNow) {
            serverOffsetMs = Number(data.serverNow) * 1000 - Date.now();
        }

        applyFormState(data);
        renderStatus([{
            ...data,
            place_id: pid,
            name: data.name || Object.keys({{ maps|tojson }}).find(
                k => {{ maps|tojson }}[k] === pid
            ) || pid
        }]);
    } catch (err) {
        notify("โหลดสถานะไม่สำเร็จ: " + err.message, false);
    }
}

async function refreshStatusOnly() {
    if (busy) return;

    const pid = $("mapSelect").value;

    try {
        if (pid === "ALL") {
            const data = await api("/api/states");
            if (data.serverNow) {
                serverOffsetMs = Number(data.serverNow) * 1000 - Date.now();
            }
            renderStatus(data.states);
        } else {
            const data = await api("/api/state/" + encodeURIComponent(pid));
            selectedState = data;

            if (data.serverNow) {
                serverOffsetMs = Number(data.serverNow) * 1000 - Date.now();
            }

            const names = {{ maps|tojson }};
            let name = pid;
            for (const [key, value] of Object.entries(names)) {
                if (value === pid) name = key;
            }

            renderStatus([{
                ...data,
                place_id: pid,
                name
            }]);
        }
    } catch (err) {
        console.error(err);
    }
}

function getFormValues() {
    const reason = $("reasonInput").value.trim();
    let seconds = Number.parseInt($("secondsInput").value, 10);

    if (!Number.isFinite(seconds)) seconds = 60;
    seconds = Math.max(1, Math.min(86400, seconds));

    return {reason, seconds};
}

async function saveConfig() {
    if (busy) return;

    const pid = $("mapSelect").value;
    const {reason, seconds} = getFormValues();

    setBusy(true);

    try {
        const data = await api("/api/save-config", {
            method:"POST",
            body:JSON.stringify({
                place_id:pid,
                reason,
                seconds
            })
        });

        notify(data.message || "บันทึกข้อมูลสำเร็จ");
        await loadSelected(true);
    } catch (err) {
        notify("บันทึกไม่สำเร็จ: " + err.message, false);
    } finally {
        setBusy(false);
    }
}

async function actionSelected(action) {
    if (busy) return;

    const pid = $("mapSelect").value;

    if (pid === "ALL") {
        notify("ตอนเลือกทุกแมพ ให้ใช้ปุ่มควบคุมทุกแมพด้านล่าง", false);
        return;
    }

    const {reason, seconds} = getFormValues();

    let mode = action;
    if (action === "scheduled" && !$("timerEnabled").checked) {
        mode = "closed";
    }

    if (mode === "closed" &&
        !confirm("ยืนยันการปิดแมพนี้ทันทีหรือไม่?")) {
        return;
    }

    setBusy(true);

    try {
        const data = await api("/api/update", {
            method:"POST",
            body:JSON.stringify({
                place_id:pid,
                mode,
                reason,
                seconds
            })
        });

        notify(data.message || "คำสั่งถูกบันทึกแล้ว");
        await loadSelected(true);
    } catch (err) {
        notify("ทำรายการไม่สำเร็จ: " + err.message, false);
    } finally {
        setBusy(false);
    }
}

async function actionAll(action) {
    if (busy) return;

    const {reason, seconds} = getFormValues();

    let mode = action;

    if (mode === "scheduled" && !$("timerEnabled").checked) {
        mode = "closed";
    }

    const message = mode === "closed"
        ? "ยืนยันปิดทุกแมพทันทีหรือไม่?"
        : mode === "scheduled"
            ? "ยืนยันเริ่ม Countdown ทุกแมพหรือไม่?"
            : "ยืนยันเปิดทุกแมพหรือไม่?";

    if (!confirm(message)) return;

    setBusy(true);

    try {
        const data = await api("/api/update-all", {
            method:"POST",
            body:JSON.stringify({
                mode,
                reason,
                seconds
            })
        });

        notify(data.message || "บันทึกทุกแมพสำเร็จ");
        await loadSelected(true);
    } catch (err) {
        notify("ทำรายการไม่สำเร็จ: " + err.message, false);
    } finally {
        setBusy(false);
    }
}

async function revealToken() {
    const pin = $("pinInput").value;

    try {
        const data = await api("/api/token", {
            method:"POST",
            body:JSON.stringify({pin})
        });

        actualToken = data.token || "";
        tokenVisible = !tokenVisible;

        $("tokenDisplay").textContent = tokenVisible
            ? actualToken
            : "••••••••••••••••••••••••••••••••";

        notify(tokenVisible
            ? "แสดง Token แล้ว"
            : "ซ่อน Token แล้ว");
    } catch (err) {
        notify("PIN ไม่ถูกต้องหรือไม่สามารถอ่าน Token ได้", false);
    }
}

async function newToken() {
    const pin = $("pinInput").value;

    if (!pin) {
        notify("กรุณากรอก Token PIN ก่อน", false);
        return;
    }

    if (!confirm(
        "สร้าง Token ใหม่หรือไม่?\n\n" +
        "Token เดิมจะใช้กับ Roblox ไม่ได้อีกต่อไป"
    )) return;

    setBusy(true);

    try {
        const data = await api("/api/new-token", {
            method:"POST",
            body:JSON.stringify({pin})
        });

        actualToken = data.token || "";
        tokenVisible = true;
        $("tokenDisplay").textContent = actualToken;

        notify("สร้าง Token ใหม่และบันทึกสำเร็จ");
    } catch (err) {
        notify("สร้าง Token ไม่สำเร็จ: " + err.message, false);
    } finally {
        setBusy(false);
    }
}

setInterval(() => {
    if (!busy) refreshStatusOnly();
}, 2000);

loadSelected();
</script>
</body>
</html>
"""


# ============================================================
# Routes
# ============================================================

@app.route("/", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        supplied = request.form.get("password", "")

        if safe_equal(supplied, ADMIN_PASSWORD):
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            app.permanent_session_lifetime = 60 * 60 * 12
            return redirect("/")

        return render_template_string(
            LOGIN_HTML,
            error="รหัสผ่านไม่ถูกต้อง",
        )

    if not logged_in():
        return render_template_string(
            LOGIN_HTML,
            error=None,
        )

    return render_template_string(
        DASHBOARD_HTML,
        maps=MAPS,
        default_reason=DEFAULT_REASON,
    )


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "database": DB_AVAILABLE,
        "time": time.time(),
        "maps": len(PLACE_IDS),
    })


@app.get("/api/state/<place_id>")
@login_required
def api_get_state(place_id):
    if place_id not in PLACE_IDS:
        return jsonify({"error": "unknown place"}), 404

    result = snapshot(place_id)

    for name, pid in MAPS.items():
        if pid == place_id:
            result["name"] = name
            break

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/states")
@login_required
def api_get_states():
    states = []

    for name, pid in MAPS.items():
        state = snapshot(pid)
        state["name"] = name
        state["place_id"] = pid
        states.append(state)

    response = jsonify({
        "states": states,
        "serverNow": time.time(),
        "database": DB_AVAILABLE,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/save-config")
@login_required
def api_save_config():
    req = request.get_json(silent=True) or {}

    pid = req.get("place_id")
    reason = normalize_reason(req.get("reason"))
    seconds = normalize_seconds(req.get("seconds", 60))

    if pid == "ALL":
        updated = []

        try:
            for place_id in PLACE_IDS:
                updated.append(
                    update_state(
                        place_id,
                        reason=reason,
                        seconds=seconds,
                    )
                )
        except Exception as exc:
            return jsonify({
                "error": str(exc),
                "success": False,
            }), 503

        return jsonify({
            "success": True,
            "message": f"บันทึกข้อมูล {len(updated)} แมพสำเร็จ",
        })

    if pid not in PLACE_IDS:
        return jsonify({"error": "unknown place"}), 404

    try:
        state = update_state(
            pid,
            reason=reason,
            seconds=seconds,
        )
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "success": False,
        }), 503

    return jsonify({
        "success": True,
        "message": "บันทึกข้อมูลสำเร็จ",
        "state": state,
    })


@app.post("/api/update")
@login_required
def api_update():
    req = request.get_json(silent=True) or {}

    pid = req.get("place_id")
    mode = req.get("mode")
    reason = req.get("reason")
    seconds = req.get("seconds")

    if pid not in PLACE_IDS:
        return jsonify({"error": "unknown place"}), 404

    if mode not in {"open", "scheduled", "closed"}:
        return jsonify({"error": "invalid mode"}), 400

    try:
        state = update_state(
            pid,
            mode=mode,
            reason=reason,
            seconds=seconds,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "success": False,
        }), 503

    return jsonify({
        "success": True,
        "message": "บันทึกคำสั่งสำเร็จ",
        "state": state,
    })


@app.post("/api/update-all")
@login_required
def api_update_all():
    req = request.get_json(silent=True) or {}

    mode = req.get("mode")
    reason = req.get("reason")
    seconds = req.get("seconds")

    if mode not in {"open", "scheduled", "closed"}:
        return jsonify({"error": "invalid mode"}), 400

    try:
        for pid in PLACE_IDS:
            update_state(
                pid,
                mode=mode,
                reason=reason,
                seconds=seconds,
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "success": False,
        }), 503

    return jsonify({
        "success": True,
        "message": f"บันทึกคำสั่ง {len(PLACE_IDS)} แมพสำเร็จ",
    })


@app.post("/api/token")
@login_required
def api_token():
    req = request.get_json(silent=True) or {}
    pin = req.get("pin")

    if not verify_token_pin(pin):
        return jsonify({"error": "invalid pin"}), 401

    ensure_db_loaded()

    with LOCK:
        if not DATA.get("token"):
            return jsonify({"error": "token unavailable"}), 503

        return jsonify({
            "success": True,
            "token": DATA["token"],
        })


@app.post("/api/new-token")
@login_required
def api_new_token():
    req = request.get_json(silent=True) or {}
    pin = req.get("pin")

    if not verify_token_pin(pin):
        return jsonify({"error": "invalid pin"}), 401

    ensure_db_loaded()

    # Save first (outside LOCK so readers are never blocked by a slow
    # database), publish to memory only after Supabase accepted it.
    with WRITE_LOCK:
        new_token = secrets.token_urlsafe(32)

        ok, error = save_with_retry(
            SYSTEM_TOKEN_ID,
            {"token": new_token},
            retries=3,
        )

        if not ok:
            return jsonify({
                "error": f"ไม่สามารถบันทึก Token ใหม่ได้: {error}",
                "success": False,
            }), 503

        with LOCK:
            DATA["token"] = new_token

    LOG.warning("Admin generated a new Roblox API token.")

    return jsonify({
        "success": True,
        "token": new_token,
    })


# ============================================================
# Roblox API
# ============================================================

@app.get("/state/<place_id>")
def roblox_state(place_id):
    """
    Roblox-facing endpoint.

    Authorization:
        Bearer <TOKEN>

    Example:
        GET /state/120651982896178
        Authorization: Bearer YOUR_TOKEN
    """
    if place_id not in PLACE_IDS:
        return jsonify({"error": "unknown place"}), 404

    ensure_db_loaded()

    supplied = request.headers.get("Authorization", "")
    token = DATA.get("token")

    if not token:
        return jsonify({"error": "service unavailable"}), 503

    if not safe_equal(supplied, f"Bearer {token}"):
        return jsonify({"error": "unauthorized"}), 401

    result = snapshot(place_id)

    # Keep this endpoint focused and stable for Roblox.
    result.pop("dbAvailable", None)
    result.pop("lastDbSync", None)

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


# ============================================================
# Startup
# ============================================================

def startup():
    LOG.info("=" * 60)
    LOG.info("Roblox Control Center starting...")
    LOG.info("Maps: %s", len(MAPS))
    LOG.info("Port: %s", PORT)
    LOG.info("Supabase: %s", SUPABASE_URL)
    LOG.info("=" * 60)

    initialize_database()

    if DATA.get("token"):
        LOG.info("API token loaded successfully.")
    else:
        LOG.error(
            "API token is unavailable. Loading will be retried automatically."
        )

    LOG.info("Startup complete.")


if __name__ == "__main__":
    startup()

    serve(
        app,
        host=HOST,
        port=PORT,
        threads=8,
        connection_limit=100,
        channel_timeout=30,
        cleanup_interval=10,
    )
