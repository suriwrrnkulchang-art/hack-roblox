import copy
import json
import os
import secrets
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from waitress import serve


# ============================================================
# CONFIG
# ============================================================

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8888"))

# ------------------------------------------------------------
# Security
# ------------------------------------------------------------
# อย่าใส่รหัสจริงลงใน source code
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "6155045")
TOKEN_PIN = os.environ.get("TOKEN_PIN", "991675788")

if not ADMIN_PASSWORD:
    raise RuntimeError(
        "กรุณาตั้ง Environment Variable: ADMIN_PASSWORD"
    )

if not TOKEN_PIN:
    raise RuntimeError(
        "กรุณาตั้ง Environment Variable: TOKEN_PIN"
    )

# ------------------------------------------------------------
# Supabase
# ------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://roiuyoflkmftbxtdeuax.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_u3rUgrL7oM5FoolIFy0ocA_MsyiuPoh")

if not SUPABASE_URL:
    raise RuntimeError(
        "กรุณาตั้ง Environment Variable: SUPABASE_URL"
    )

if not SUPABASE_KEY:
    raise RuntimeError(
        "กรุณาตั้ง Environment Variable: SUPABASE_KEY"
    )

# ------------------------------------------------------------
# Maps
# ------------------------------------------------------------

MAPS = {
    "Place 1": "120651982896178",
    "Place 2": "77210125175879",
}

DEFAULT_REASON = "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง"
DEFAULT_SECONDS = 60

# จำกัดเวลาไม่ให้กรอกค่าผิดปกติ
MIN_SECONDS = 1
MAX_SECONDS = 7 * 24 * 60 * 60

# ------------------------------------------------------------
# Local backup
# ------------------------------------------------------------

BACKUP_FILE = os.environ.get(
    "CONTROL_BACKUP_FILE",
    "control_backup.json"
)

# ------------------------------------------------------------
# Runtime
# ------------------------------------------------------------

LOCK = threading.RLock()

SUPABASE_TIMEOUT = 8
SUPABASE_RETRIES = 3

DATA = {
    "token": None,
    "places": {},
}

SYSTEM_TOKEN_ID = "__SYS_TOKEN__"


# ============================================================
# DEFAULT STATE
# ============================================================

def default_state():
    return {
        "mode": "open",
        "reason": DEFAULT_REASON,
        "deadline": None,
        "seconds": DEFAULT_SECONDS,
    }


# ============================================================
# UTILITY
# ============================================================

def now_ts():
    return time.time()


def iso_time(timestamp):
    if not timestamp:
        return None

    try:
        return datetime.fromtimestamp(
            float(timestamp),
            tz=timezone.utc
        ).isoformat()
    except Exception:
        return None


def safe_int(value, default=DEFAULT_SECONDS):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default

    return max(MIN_SECONDS, min(MAX_SECONDS, value))


def normalize_reason(reason):
    if reason is None:
        return DEFAULT_REASON

    reason = str(reason).strip()

    if not reason:
        return DEFAULT_REASON

    # ป้องกันข้อความยาวเกินจำเป็น
    return reason[:500]


def make_backup_data():
    return {
        "token": DATA["token"],
        "places": copy.deepcopy(DATA["places"]),
    }


# ============================================================
# LOCAL BACKUP
# ============================================================

def save_local_backup():
    """
    บันทึก backup แบบ atomic

    เขียนไฟล์ temporary ก่อน
    แล้วค่อย os.replace()

    ทำให้โอกาสไฟล์ JSON เสียระหว่างเขียนลดลงมาก
    """

    temp_file = BACKUP_FILE + ".tmp"

    try:
        payload = make_backup_data()

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                payload,
                f,
                ensure_ascii=False,
                indent=2
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_file, BACKUP_FILE)

        return True

    except Exception as exc:
        print(f"[LOCAL BACKUP ERROR] {exc}")

        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass

        return False


def load_local_backup():
    try:
        if not os.path.exists(BACKUP_FILE):
            return None

        with open(
            BACKUP_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return None

        return data

    except Exception as exc:
        print(f"[LOCAL LOAD ERROR] {exc}")
        return None


# ============================================================
# SUPABASE HTTP
# ============================================================

def supabase_request(
    method,
    endpoint,
    payload=None,
    query="",
    timeout=SUPABASE_TIMEOUT
):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"

    if query:
        url += "?" + query

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(
        req,
        timeout=timeout
    ) as response:

        raw = response.read()

        if not raw:
            return True, None

        try:
            return True, json.loads(
                raw.decode("utf-8")
            )
        except json.JSONDecodeError:
            return True, None


# ============================================================
# LOAD FROM SUPABASE
# ============================================================

def load_from_supabase():
    """
    โหลดข้อมูลจาก Supabase

    สำคัญ:
    ถ้า Supabase ติดต่อไม่ได้ จะ return None
    และระบบจะไม่สร้างค่า default ไปทับข้อมูลเดิม
    """

    for attempt in range(1, SUPABASE_RETRIES + 1):

        try:
            ok, data = supabase_request(
                "GET",
                "control_state",
                query="select=*"
            )

            if not ok:
                raise RuntimeError("Supabase request failed")

            if not isinstance(data, list):
                raise RuntimeError(
                    "Supabase returned invalid data"
                )

            token = None
            places = {}

            for row in data:

                place_id = row.get("place_id")

                if not place_id:
                    continue

                if place_id == SYSTEM_TOKEN_ID:
                    token = row.get("reason")
                    continue

                if place_id not in MAPS.values():
                    continue

                places[place_id] = {
                    "mode": row.get("mode") or "open",
                    "reason": normalize_reason(
                        row.get("reason")
                    ),
                    "deadline": row.get("deadline"),
                    "seconds": safe_int(
                        row.get("seconds"),
                        DEFAULT_SECONDS
                    ),
                }

            return {
                "token": token,
                "places": places,
            }

        except Exception as exc:

            print(
                f"[SUPABASE LOAD] "
                f"attempt {attempt}/{SUPABASE_RETRIES}: {exc}"
            )

            if attempt < SUPABASE_RETRIES:
                time.sleep(0.5 * attempt)

    return None


# ============================================================
# SAVE ONE RECORD TO SUPABASE
# ============================================================

def save_record_to_supabase(
    place_id,
    state_data
):
    """
    UPSERT โดยใช้ place_id เป็น conflict key

    ต้องมี UNIQUE หรือ PRIMARY KEY ที่ place_id
    """

    payload = {
        "place_id": place_id,
        "mode": state_data.get("mode"),
        "reason": state_data.get("reason"),
        "deadline": state_data.get("deadline"),
        "seconds": state_data.get(
            "seconds",
            DEFAULT_SECONDS
        ),
    }

    for attempt in range(1, SUPABASE_RETRIES + 1):

        try:

            ok, _ = supabase_request(
                "POST",
                "control_state",
                payload=payload,
                query="on_conflict=place_id",
            )

            if ok:
                return True

        except Exception as exc:

            print(
                f"[SUPABASE SAVE] "
                f"{place_id} "
                f"attempt {attempt}/{SUPABASE_RETRIES}: {exc}"
            )

        if attempt < SUPABASE_RETRIES:
            time.sleep(0.5 * attempt)

    return False


# ============================================================
# SAVE STATE SAFELY
# ============================================================

def persist_state(place_id, state):
    """
    บันทึก Supabase ก่อน

    ถ้าสำเร็จ:
        อัปเดต DATA
        แล้วสร้าง local backup

    ถ้า Supabase ล้ม:
        ไม่เปลี่ยน DATA
        return False

    จุดนี้แก้ปัญหาใหญ่ของโค้ดเดิม:
    เดิม DATA ถูกเปลี่ยนก่อน แล้วค่อยลอง save
    """

    if not save_record_to_supabase(
        place_id,
        state
    ):
        return False

    DATA["places"][place_id] = copy.deepcopy(state)

    # Local backup เป็นชั้นสำรอง
    save_local_backup()

    return True


# ============================================================
# INITIALIZE DATA
# ============================================================

def initialize_data():

    remote = load_from_supabase()

    local = load_local_backup()

    # --------------------------------------------------------
    # Remote สำเร็จ
    # --------------------------------------------------------

    if remote is not None:

        print("[STARTUP] Loaded from Supabase")

        token = remote.get("token")

        if not token:
            token = secrets.token_urlsafe(32)

            save_record_to_supabase(
                SYSTEM_TOKEN_ID,
                {
                    "mode": "token",
                    "reason": token,
                    "deadline": None,
                    "seconds": DEFAULT_SECONDS,
                }
            )

        DATA["token"] = token

        for place_id in MAPS.values():

            state = remote["places"].get(
                place_id
            )

            if state is None:

                state = default_state()

                if save_record_to_supabase(
                    place_id,
                    state
                ):
                    print(
                        f"[STARTUP] Created missing "
                        f"state: {place_id}"
                    )

            DATA["places"][place_id] = state

        save_local_backup()

        return

    # --------------------------------------------------------
    # Supabase ล่ม → ใช้ Local Backup
    # --------------------------------------------------------

    print(
        "[STARTUP] Supabase unavailable. "
        "Trying local backup..."
    )

    if local is not None:

        DATA["token"] = (
            local.get("token")
            or secrets.token_urlsafe(32)
        )

        local_places = local.get(
            "places",
            {}
        )

        for place_id in MAPS.values():

            DATA["places"][place_id] = copy.deepcopy(
                local_places.get(
                    place_id,
                    default_state()
                )
            )

        print(
            "[STARTUP] Local backup loaded."
        )

        return

    # --------------------------------------------------------
    # ไม่มีทั้ง Remote และ Local
    #
    # สร้างค่าใหม่ได้เฉพาะกรณีนี้
    # --------------------------------------------------------

    print(
        "[STARTUP] No database/backup found. "
        "Creating fresh state."
    )

    DATA["token"] = secrets.token_urlsafe(32)

    for place_id in MAPS.values():
        DATA["places"][place_id] = default_state()

    # พยายามสร้างข้อมูลใน Supabase
    save_record_to_supabase(
        SYSTEM_TOKEN_ID,
        {
            "mode": "token",
            "reason": DATA["token"],
            "deadline": None,
            "seconds": DEFAULT_SECONDS,
        }
    )

    for place_id in MAPS.values():
        save_record_to_supabase(
            place_id,
            DATA["places"][place_id]
        )

    save_local_backup()


initialize_data()


# ============================================================
# STATE LOGIC
# ============================================================

def reconcile_locked():
    """
    ตรวจ scheduled ที่หมดเวลา

    ไม่บันทึกทุก request ถ้าไม่มีการเปลี่ยนแปลง
    """

    current = now_ts()

    for place_id, state in DATA["places"].items():

        if (
            state.get("mode") == "scheduled"
            and state.get("deadline") is not None
            and current >= float(state["deadline"])
        ):

            new_state = copy.deepcopy(state)

            new_state["mode"] = "closed"
            new_state["deadline"] = None

            if save_record_to_supabase(
                place_id,
                new_state
            ):

                DATA["places"][place_id] = new_state

                print(
                    f"[AUTO CLOSE] {place_id}"
                )

                save_local_backup()


def snapshot(place_id):

    with LOCK:

        reconcile_locked()

        state = copy.deepcopy(
            DATA["places"][place_id]
        )

        server_now = now_ts()

        result = {
            **state,
            "serverNow": server_now,
            "serverTimeISO": iso_time(server_now),
        }

        if (
            state["mode"] == "scheduled"
            and state.get("deadline") is not None
        ):

            deadline = float(
                state["deadline"]
            )

            seconds_total = safe_int(
                state.get(
                    "seconds",
                    DEFAULT_SECONDS
                )
            )

            started_at = (
                deadline - seconds_total
            )

            elapsed = max(
                0,
                server_now - started_at
            )

            remaining = max(
                0,
                deadline - server_now
            )

            result["startedAt"] = started_at
            result["startedAtISO"] = iso_time(
                started_at
            )

            result["deadlineISO"] = iso_time(
                deadline
            )

            result["elapsedSeconds"] = int(
                elapsed
            )

            result["remainingSeconds"] = int(
                remaining
            )

        else:

            result["startedAt"] = None
            result["startedAtISO"] = None
            result["deadlineISO"] = None
            result["elapsedSeconds"] = None
            result["remainingSeconds"] = None

        return result


def update_state(
    place_id,
    mode=None,
    reason=None,
    seconds=None
):

    with LOCK:

        if place_id not in DATA["places"]:
            return False, "unknown place"

        old_state = copy.deepcopy(
            DATA["places"][place_id]
        )

        new_state = copy.deepcopy(
            old_state
        )

        if reason is not None:
            new_state["reason"] = normalize_reason(
                reason
            )

        if seconds is not None:
            new_state["seconds"] = safe_int(
                seconds
            )

        if mode is not None:

            new_state["mode"] = mode

            if mode == "scheduled":

                duration = safe_int(
                    seconds
                    if seconds is not None
                    else new_state.get(
                        "seconds",
                        DEFAULT_SECONDS
                    )
                )

                new_state["seconds"] = duration
                new_state["deadline"] = (
                    now_ts() + duration
                )

            else:

                new_state["deadline"] = None

        # ----------------------------------------------------
        # ตรวจว่ามีการเปลี่ยนแปลงจริงไหม
        # ----------------------------------------------------

        if new_state == old_state:
            return True, "no_change"

        # ----------------------------------------------------
        # Save ก่อน mutate DATA
        # ----------------------------------------------------

        if not save_record_to_supabase(
            place_id,
            new_state
        ):
            return False, "supabase_save_failed"

        # ----------------------------------------------------
        # Commit memory
        # ----------------------------------------------------

        DATA["places"][place_id] = new_state

        # ----------------------------------------------------
        # Backup
        # ----------------------------------------------------

        backup_ok = save_local_backup()

        if not backup_ok:
            print(
                "[WARNING] Supabase saved, "
                "but local backup failed."
            )

        return True, "saved"


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY"
)

if not app.secret_key:
    raise RuntimeError(
        "กรุณาตั้ง Environment Variable: FLASK_SECRET_KEY"
    )

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE",
        "0"
    ) == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


# ============================================================
# AUTH
# ============================================================

def is_admin():

    return session.get(
        "admin_authenticated",
        False
    ) is True


def admin_required():

    if not is_admin():
        return jsonify({
            "success": False,
            "error": "unauthorized"
        }), 401

    return None


# ============================================================
# LOGIN HTML
# ============================================================

LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Roblox Control Center - Login</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;

    display: flex;
    align-items: center;
    justify-content: center;

    font-family:
        Inter,
        "Segoe UI",
        Tahoma,
        sans-serif;

    color: #f5f7ff;

    background:
        radial-gradient(
            circle at top,
            #202a4d 0,
            #0b0f1a 45%,
            #06080d 100%
        );
}

.login-card {
    width: min(420px, calc(100% - 32px));

    padding: 32px;

    border: 1px solid rgba(255,255,255,.08);

    border-radius: 22px;

    background:
        rgba(17, 22, 36, .92);

    box-shadow:
        0 30px 80px rgba(0,0,0,.45);

    backdrop-filter: blur(20px);
}

.logo {
    width: 64px;
    height: 64px;

    margin: 0 auto 20px;

    display: grid;
    place-items: center;

    border-radius: 18px;

    background:
        linear-gradient(
            135deg,
            #5865f2,
            #7c3aed
        );

    font-size: 28px;

    box-shadow:
        0 15px 35px rgba(88,101,242,.3);
}

h1 {
    margin: 0;
    text-align: center;
    font-size: 24px;
}

.subtitle {
    margin: 8px 0 26px;
    text-align: center;
    color: #8f9bb5;
    font-size: 14px;
}

.input {
    width: 100%;
    padding: 14px 15px;

    border: 1px solid #293149;
    border-radius: 12px;

    background: #0d1220;
    color: white;

    outline: none;

    font-size: 15px;
}

.input:focus {
    border-color: #6675ff;

    box-shadow:
        0 0 0 3px
        rgba(102,117,255,.12);
}

.button {
    width: 100%;

    margin-top: 14px;

    padding: 14px;

    border: 0;
    border-radius: 12px;

    color: white;

    background:
        linear-gradient(
            135deg,
            #5865f2,
            #7c3aed
        );

    font-weight: 700;

    cursor: pointer;

    transition: .2s;
}

.button:hover {
    transform: translateY(-1px);
    filter: brightness(1.08);
}

.error {
    margin-bottom: 15px;

    padding: 12px;

    border: 1px solid rgba(255,80,100,.2);
    border-radius: 10px;

    color: #ff8f9c;

    background: rgba(255,70,90,.08);

    font-size: 14px;
}

.footer {
    margin-top: 20px;

    text-align: center;

    color: #667089;

    font-size: 12px;
}

</style>
</head>

<body>

<div class="login-card">

    <div class="logo">🛡️</div>

    <h1>Roblox Control Center</h1>

    <div class="subtitle">
        Secure Server Management Panel
    </div>

    {% if error %}
        <div class="error">
            ⚠️ {{ error }}
        </div>
    {% endif %}

    <form method="POST">

        <input
            class="input"
            type="password"
            name="password"
            placeholder="รหัสผ่านแอดมิน"
            autocomplete="current-password"
            required
            autofocus
        >

        <button
            class="button"
            type="submit"
        >
            🔐 เข้าสู่ระบบ
        </button>

    </form>

    <div class="footer">
        Protected Control System
    </div>

</div>

</body>
</html>
"""


# ============================================================
# DASHBOARD HTML
# ============================================================

HTML_TEMPLATE = r"""
<!DOCTYPE html>

<html lang="th">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<title>Roblox Control Center</title>

<style>

* {
    box-sizing: border-box;
}

:root {
    --bg: #070a12;
    --panel: #101522;
    --panel2: #0c111d;

    --border: rgba(255,255,255,.08);

    --text: #f4f7ff;
    --muted: #8792aa;

    --blue: #5865f2;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #f59e0b;
    --cyan: #06b6d4;
}

body {

    margin: 0;

    min-height: 100vh;

    font-family:
        Inter,
        "Segoe UI",
        Tahoma,
        sans-serif;

    color: var(--text);

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(88,101,242,.18),
            transparent 35%
        ),
        radial-gradient(
            circle at 100% 20%,
            rgba(6,182,212,.08),
            transparent 30%
        ),
        var(--bg);

    padding: 24px;
}

.container {

    width: min(
        100%,
        1100px
    );

    margin: auto;
}

.header {

    display: flex;

    justify-content: space-between;
    align-items: center;

    gap: 20px;

    margin-bottom: 22px;

    padding: 22px;

    border: 1px solid var(--border);

    border-radius: 20px;

    background:
        linear-gradient(
            135deg,
            rgba(18,25,42,.96),
            rgba(10,14,24,.96)
        );

    box-shadow:
        0 20px 60px rgba(0,0,0,.25);
}

.brand {

    display: flex;

    align-items: center;

    gap: 14px;
}

.logo {

    width: 54px;
    height: 54px;

    display: grid;
    place-items: center;

    border-radius: 16px;

    background:
        linear-gradient(
            135deg,
            #5865f2,
            #7c3aed
        );

    font-size: 25px;
}

.brand h1 {

    margin: 0;

    font-size: 21px;
}

.brand p {

    margin: 4px 0 0;

    color: var(--muted);

    font-size: 13px;
}

.logout {

    padding: 10px 14px;

    border: 1px solid rgba(239,68,68,.25);

    border-radius: 10px;

    color: #ff8c98;

    background: rgba(239,68,68,.08);

    text-decoration: none;

    font-weight: 600;
}

.grid {

    display: grid;

    grid-template-columns:
        minmax(0, 1fr)
        minmax(0, 1fr);

    gap: 18px;
}

.card {

    padding: 20px;

    border: 1px solid var(--border);

    border-radius: 18px;

    background:
        linear-gradient(
            180deg,
            rgba(17,23,38,.96),
            rgba(11,15,26,.96)
        );

    box-shadow:
        0 15px 45px rgba(0,0,0,.18);
}

.card.full {

    grid-column: 1 / -1;
}

.card-title {

    display: flex;

    align-items: center;

    justify-content: space-between;

    gap: 10px;

    margin-bottom: 16px;
}

.card-title h2 {

    margin: 0;

    font-size: 16px;
}

.card-title span {

    color: var(--muted);

    font-size: 12px;
}

label {

    display: block;

    margin-bottom: 7px;

    color: #aeb8ce;

    font-size: 13px;

    font-weight: 600;
}

.form-group {

    margin-bottom: 16px;
}

select,
input {

    width: 100%;

    padding: 12px 13px;

    border: 1px solid #293149;

    border-radius: 11px;

    outline: none;

    background: #0b101c;

    color: white;

    font-size: 14px;
}

select:focus,
input:focus {

    border-color: #6675ff;

    box-shadow:
        0 0 0 3px
        rgba(102,117,255,.10);
}

.reason-row {

    display: flex;

    gap: 9px;
}

.reason-row input {

    flex: 1;
}

button {

    border: 0;

    border-radius: 11px;

    padding: 12px 14px;

    color: white;

    font-weight: 700;

    cursor: pointer;

    transition:
        transform .15s,
        filter .15s,
        opacity .15s;
}

button:hover {

    filter: brightness(1.08);

    transform: translateY(-1px);
}

button:disabled {

    opacity: .45;

    cursor: not-allowed;

    transform: none;
}

.btn-save {

    background: #2563eb;

    white-space: nowrap;
}

.actions {

    display: grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap: 10px;
}

.btn-close {

    background:
        linear-gradient(
            135deg,
            #dc2626,
            #ef4444
        );
}

.btn-cancel {

    background:
        linear-gradient(
            135deg,
            #d97706,
            #f59e0b
        );
}

.btn-open {

    background:
        linear-gradient(
            135deg,
            #16a34a,
            #22c55e
        );
}

.all-actions {

    display: grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap: 10px;

    margin-top: 10px;
}

.btn-all-close {

    background:
        linear-gradient(
            135deg,
            #991b1b,
            #dc2626
        );
}

.btn-all-cancel {

    background:
        linear-gradient(
            135deg,
            #b45309,
            #d97706
        );
}

.btn-all-open {

    background:
        linear-gradient(
            135deg,
            #0f766e,
            #06b6d4
        );
}

.timer-row {

    display: flex;

    align-items: center;

    gap: 10px;

    padding: 12px;

    border: 1px solid var(--border);

    border-radius: 12px;

    background: rgba(255,255,255,.025);

    margin-bottom: 15px;
}

.timer-row input {

    width: auto;

    min-width: 130px;
}

.checkbox {

    width: 18px;
    height: 18px;

    accent-color: var(--blue);
}

.status {

    position: relative;

    overflow: hidden;

    padding: 20px;

    border-radius: 16px;

    border: 1px solid var(--border);

    background: #090e19;
}

.status-header {

    display: flex;

    justify-content: space-between;

    align-items: center;

    gap: 12px;

    margin-bottom: 18px;
}

.status-name {

    font-size: 18px;

    font-weight: 800;
}

.badge {

    display: inline-flex;

    align-items: center;

    gap: 7px;

    padding: 7px 11px;

    border-radius: 999px;

    font-size: 12px;

    font-weight: 800;
}

.badge.open {

    color: #86efac;

    background:
        rgba(34,197,94,.10);

    border: 1px solid
        rgba(34,197,94,.18);
}

.badge.scheduled {

    color: #fde68a;

    background:
        rgba(245,158,11,.10);

    border: 1px solid
        rgba(245,158,11,.18);
}

.badge.closed {

    color: #fca5a5;

    background:
        rgba(239,68,68,.10);

    border: 1px solid
        rgba(239,68,68,.18);
}

.badge.loading {

    color: #93c5fd;

    background:
        rgba(59,130,246,.10);
}

.status-grid {

    display: grid;

    grid-template-columns:
        repeat(2, 1fr);

    gap: 10px;
}

.info {

    padding: 13px;

    border-radius: 12px;

    background:
        rgba(255,255,255,.025);

    border: 1px solid
        rgba(255,255,255,.045);
}

.info .label {

    color: #68748d;

    font-size: 11px;

    margin-bottom: 5px;
}

.info .value {

    color: #e9edfa;

    font-size: 13px;

    font-weight: 650;

    word-break: break-word;
}

.countdown {

    margin: 18px 0 5px;

    padding: 18px;

    text-align: center;

    border-radius: 15px;

    background:
        linear-gradient(
            135deg,
            rgba(88,101,242,.12),
            rgba(6,182,212,.07)
        );

    border: 1px solid
        rgba(88,101,242,.15);
}

.countdown-label {

    color: #8792aa;

    font-size: 12px;

    margin-bottom: 5px;
}

.countdown-time {

    font-family:
        "Cascadia Code",
        Consolas,
        monospace;

    font-size: 32px;

    font-weight: 900;

    letter-spacing: 1px;
}

.reason-display {

    margin-top: 12px;

    padding: 12px;

    border-left: 3px solid var(--blue);

    border-radius: 7px;

    background:
        rgba(88,101,242,.05);

    color: #b9c2d7;

    font-size: 13px;

    line-height: 1.5;
}

.connection {

    display: flex;

    align-items: center;

    gap: 8px;

    margin-top: 14px;

    color: #78849d;

    font-size: 12px;
}

.dot {

    width: 8px;
    height: 8px;

    border-radius: 50%;

    background: #22c55e;

    box-shadow:
        0 0 12px rgba(34,197,94,.6);
}

.dot.error {

    background: #ef4444;

    box-shadow:
        0 0 12px rgba(239,68,68,.6);
}

.token-section {

    margin-top: 18px;
}

.token-controls {

    display: flex;

    gap: 9px;

    margin-top: 10px;
}

.token-controls input {

    flex: 1;
}

.token-button {

    min-width: 100px;

    background: #7c3aed;
}

.token-new {

    background: #b45309;
}

.token-display {

    padding: 13px;

    border-radius: 11px;

    background: #070b13;

    border: 1px solid var(--border);

    color: #67e8f9;

    font-family:
        "Cascadia Code",
        Consolas,
        monospace;

    word-break: break-all;

    font-size: 12px;
}

.toast {

    position: fixed;

    right: 20px;
    bottom: 20px;

    z-index: 9999;

    max-width: 360px;

    padding: 14px 16px;

    border-radius: 13px;

    color: white;

    background: #111827;

    border: 1px solid var(--border);

    box-shadow:
        0 20px 50px rgba(0,0,0,.4);

    transform:
        translateY(20px);

    opacity: 0;

    pointer-events: none;

    transition: .25s;
}

.toast.show {

    transform:
        translateY(0);

    opacity: 1;
}

.toast.success {

    border-color:
        rgba(34,197,94,.25);
}

.toast.error {

    border-color:
        rgba(239,68,68,.25);
}

@media (max-width: 800px) {

    body {
        padding: 12px;
    }

    .grid {
        grid-template-columns: 1fr;
    }

    .card.full {
        grid-column: auto;
    }

    .actions,
    .all-actions {
        grid-template-columns: 1fr;
    }

    .header {
        align-items: flex-start;
    }

    .reason-row {
        flex-direction: column;
    }

    .token-controls {
        flex-direction: column;
    }

    .status-grid {
        grid-template-columns: 1fr;
    }
}

</style>

</head>

<body>

<div class="container">

    <!-- HEADER -->

    <header class="header">

        <div class="brand">

            <div class="logo">
                🛡️
            </div>

            <div>

                <h1>
                    Roblox Control Center
                </h1>

                <p>
                    Server Management Dashboard
                </p>

            </div>

        </div>

        <a
            class="logout"
            href="/logout"
        >
            ออกจากระบบ
        </a>

    </header>


    <div class="grid">


        <!-- CONFIG -->

        <section class="card">

            <div class="card-title">

                <h2>
                    ⚙️ ตั้งค่าการควบคุม
                </h2>

                <span>
                    Control
                </span>

            </div>


            <div class="form-group">

                <label>
                    แมพที่ต้องการจัดการ
                </label>

                <select
                    id="mapSelect"
                    onchange="loadMapState()"
                >

                    <option value="ALL">
                        🌐 ทุกแมพ
                    </option>

                    {% for name, pid in maps.items() %}

                    <option value="{{ pid }}">
                        {{ name }} — {{ pid }}
                    </option>

                    {% endfor %}

                </select>

            </div>


            <div class="form-group">

                <label>
                    เหตุผล / ข้อความแจ้งเตือน
                </label>

                <div class="reason-row">

                    <input
                        id="reasonInput"
                        type="text"
                        maxlength="500"
                        value="{{ default_reason }}"
                    >

                    <button
                        class="btn-save"
                        onclick="saveConfigOnly()"
                    >
                        💾 บันทึก
                    </button>

                </div>

            </div>


            <div class="timer-row">

                <input
                    class="checkbox"
                    id="timerEnabled"
                    type="checkbox"
                    checked
                    onchange="toggleTimerInput()"
                >

                <div style="flex:1">

                    <label
                        for="timerEnabled"
                        style="margin:0"
                    >
                        เปิดระบบนับถอยหลังก่อนปิด
                    </label>

                </div>

                <input
                    id="secondsInput"
                    type="number"
                    min="{{ min_seconds }}"
                    max="{{ max_seconds }}"
                    value="{{ default_seconds }}"
                >

                <span style="color:#78849d;font-size:12px">
                    วินาที
                </span>

            </div>


            <div class="actions">

                <button
                    class="btn-close"
                    onclick="sendAction('scheduled')"
                >
                    🛑 เริ่มปิด
                </button>

                <button
                    class="btn-cancel"
                    onclick="sendAction('open-cancel')"
                >
                    ↩ ยกเลิก
                </button>

                <button
                    class="btn-open"
                    onclick="sendAction('open')"
                >
                    ✅ เปิดแมพ
                </button>

            </div>

        </section>


        <!-- STATUS -->

        <section class="card">

            <div class="card-title">

                <h2>
                    📊 สถานะระบบ
                </h2>

                <span id="lastUpdate">
                    กำลังโหลด...
                </span>

            </div>

            <div
                class="status"
                id="statusView"
            >

                กำลังโหลดสถานะ...

            </div>

        </section>


        <!-- ALL MAPS -->

        <section class="card full">

            <div class="card-title">

                <h2>
                    🌐 ควบคุมทุกแมพ
                </h2>

                <span>
                    Global Control
                </span>

            </div>

            <div class="all-actions">

                <button
                    class="btn-all-close"
                    onclick="sendActionAll('scheduled')"
                >
                    🚨 ปิดทุกแมพ
                </button>

                <button
                    class="btn-all-cancel"
                    onclick="sendActionAll('open-cancel')"
                >
                    ↩ ยกเลิกการปิดทั้งหมด
                </button>

                <button
                    class="btn-all-open"
                    onclick="sendActionAll('open')"
                >
                    🟢 เปิดทุกแมพ
                </button>

            </div>

        </section>


        <!-- TOKEN -->

        <section class="card full">

            <div class="card-title">

                <h2>
                    🔑 API Token
                </h2>

                <span>
                    Roblox API Authentication
                </span>

            </div>

            <div class="token-display" id="tokenDisplay">
                ••••••••••••••••••••••••••••••••
            </div>

            <div class="token-controls">

                <input
                    id="pinInput"
                    type="password"
                    placeholder="PIN สำหรับจัดการ Token"
                    autocomplete="off"
                >

                <button
                    class="token-button"
                    onclick="toggleToken()"
                >
                    👁️ แสดง / ซ่อน
                </button>

                <button
                    class="token-new"
                    onclick="generateNewToken()"
                >
                    🔄 สร้าง Token ใหม่
                </button>

            </div>

            <div class="connection">

                <span
                    class="dot"
                    id="connectionDot"
                ></span>

                <span id="connectionText">
                    ระบบพร้อมใช้งาน
                </span>

            </div>

        </section>

    </div>

</div>


<div
    class="toast"
    id="toast"
></div>


<script>

let currentState = null;
let serverOffset = 0;
let tokenVisible = false;


// ============================================================
// TOAST
// ============================================================

function showToast(
    message,
    type = "success"
) {

    const toast =
        document.getElementById("toast");

    toast.innerText = message;

    toast.className =
        "toast show " + type;

    setTimeout(() => {

        toast.className =
            "toast";

    }, 3500);
}


// ============================================================
// API HELPER
// ============================================================

async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            {
                ...options,

                headers: {
                    "Content-Type":
                        "application/json",

                    ...(options.headers || {})
                },

                cache: "no-store"
            }
        );

    let data = null;

    try {
        data = await response.json();
    }
    catch {
        data = {};
    }

    if (!response.ok) {

        const message =
            data.error ||
            `HTTP ${response.status}`;

        throw new Error(message);
    }

    return data;
}


// ============================================================
// TIMER INPUT
// ============================================================

function toggleTimerInput() {

    const enabled =
        document.getElementById(
            "timerEnabled"
        ).checked;

    const input =
        document.getElementById(
            "secondsInput"
        );

    input.disabled = !enabled;
}


// ============================================================
// FORMAT TIME
// ============================================================

function formatDuration(seconds) {

    seconds = Math.max(
        0,
        Math.floor(seconds)
    );

    const days =
        Math.floor(
            seconds / 86400
        );

    seconds %= 86400;

    const hours =
        Math.floor(
            seconds / 3600
        );

    seconds %= 3600;

    const minutes =
        Math.floor(
            seconds / 60
        );

    seconds %= 60;

    if (days > 0) {

        return `${days}วัน ` +
               `${String(hours).padStart(2,"0")}:` +
               `${String(minutes).padStart(2,"0")}:` +
               `${String(seconds).padStart(2,"0")}`;

    }

    return (
        `${String(hours).padStart(2,"0")}:` +
        `${String(minutes).padStart(2,"0")}:` +
        `${String(seconds).padStart(2,"0")}`
    );
}


function formatDate(timestamp) {

    if (!timestamp) {
        return "-";
    }

    const date =
        new Date(
            timestamp * 1000
        );

    return date.toLocaleString(
        "th-TH",
        {
            dateStyle: "medium",
            timeStyle: "medium"
        }
    );
}


// ============================================================
// STATUS
// ============================================================

function statusBadge(mode) {

    if (mode === "open") {

        return `
            <span class="badge open">
                🟢 เปิดให้บริการ
            </span>
        `;
    }

    if (mode === "scheduled") {

        return `
            <span class="badge scheduled">
                🟡 กำลังนับถอยหลัง
            </span>
        `;
    }

    return `
        <span class="badge closed">
            🔴 ปิดปรับปรุง
        </span>
    `;
}


function renderStatus(data) {

    currentState = data;

    serverOffset =
        (data.serverNow * 1000)
        - Date.now();

    const status =
        document.getElementById(
            "statusView"
        );

    let countdown = "";

    if (
        data.mode === "scheduled" &&
        data.remainingSeconds !== null
    ) {

        countdown = `
            <div class="countdown">

                <div class="countdown-label">
                    เวลาที่เหลือก่อนปิด
                </div>

                <div
                    class="countdown-time"
                    id="countdownTime"
                >
                    ${formatDuration(
                        data.remainingSeconds
                    )}
                </div>

            </div>
        `;

    }
    else if (
        data.mode === "closed"
    ) {

        countdown = `
            <div class="countdown">

                <div class="countdown-label">
                    สถานะปัจจุบัน
                </div>

                <div class="countdown-time">
                    ปิดอยู่
                </div>

            </div>
        `;

    }
    else {

        countdown = `
            <div class="countdown">

                <div class="countdown-label">
                    สถานะปัจจุบัน
                </div>

                <div class="countdown-time">
                    พร้อมให้บริการ
                </div>

            </div>
        `;
    }


    status.innerHTML = `

        <div class="status-header">

            <div class="status-name">
                Roblox Server
            </div>

            ${statusBadge(data.mode)}

        </div>


        <div class="status-grid">

            <div class="info">

                <div class="label">
                    MODE
                </div>

                <div class="value">
                    ${data.mode}
                </div>

            </div>


            <div class="info">

                <div class="label">
                    ระยะเวลาที่ตั้งไว้
                </div>

                <div class="value">
                    ${data.seconds} วินาที
                </div>

            </div>


            <div class="info">

                <div class="label">
                    เริ่มนับถอยหลัง
                </div>

                <div class="value">
                    ${
                        data.startedAt
                        ? formatDate(
                            data.startedAt
                        )
                        : "-"
                    }
                </div>

            </div>


            <div class="info">

                <div class="label">
                    เวลาสิ้นสุด
                </div>

                <div class="value">
                    ${
                        data.deadline
                        ? formatDate(
                            data.deadline
                        )
                        : "-"
                    }
                </div>

            </div>

        </div>


        ${countdown}


        <div class="info">

            <div class="label">
                เวลาเซิร์ฟเวอร์
            </div>

            <div class="value">
                ${formatDate(
                    data.serverNow
                )}
            </div>

        </div>


        <div class="reason-display">

            <strong>
                📝 เหตุผล:
            </strong>

            ${escapeHtml(
                data.reason
            )}

        </div>


        <div class="connection">

            <span class="dot"></span>

            <span>
                ได้รับข้อมูลจาก Server สำเร็จ
            </span>

        </div>

    `;

    document.getElementById(
        "lastUpdate"
    ).innerText =
        "อัปเดต " +
        new Date().toLocaleTimeString(
            "th-TH"
        );
}


// ============================================================
// HTML ESCAPE
// ============================================================

function escapeHtml(text) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text ?? "";

    return div.innerHTML;
}


// ============================================================
// LOAD STATE
// ============================================================

async function loadMapState() {

    const pid =
        document.getElementById(
            "mapSelect"
        ).value;


    if (pid === "ALL") {

        document.getElementById(
            "statusView"
        ).innerHTML = `

            <div class="status-header">

                <div class="status-name">
                    🌐 ทุกแมพ
                </div>

                <span class="badge loading">
                    กำลังดูภาพรวม
                </span>

            </div>

            <div class="reason-display">
                เลือกแมพเฉพาะตัว
                เพื่อดูตัวจับเวลาและรายละเอียด
                ของแมพนั้นแบบละเอียด
            </div>

        `;

        return;
    }


    try {

        const data =
            await api(
                "/api/state/" + pid
            );

        document.getElementById(
            "reasonInput"
        ).value =
            data.reason || "";


        if (data.seconds) {

            document.getElementById(
                "secondsInput"
            ).value =
                data.seconds;
        }


        renderStatus(data);

        setConnection(
            true,
            "เชื่อมต่อ Server สำเร็จ"
        );

    }
    catch (error) {

        console.error(error);

        setConnection(
            false,
            "ไม่สามารถโหลดสถานะได้"
        );

        showToast(
            "ไม่สามารถโหลดสถานะ: " +
            error.message,
            "error"
        );
    }
}


// ============================================================
// CONNECTION
// ============================================================

function setConnection(
    connected,
    text
) {

    const dot =
        document.getElementById(
            "connectionDot"
        );

    dot.classList.toggle(
        "error",
        !connected
    );

    document.getElementById(
        "connectionText"
    ).innerText =
        text;
}


// ============================================================
// SAVE CONFIG
// ============================================================

async function saveConfigOnly() {

    const pid =
        document.getElementById(
            "mapSelect"
        ).value;

    const reason =
        document.getElementById(
            "reasonInput"
        ).value.trim();

    const seconds =
        Number(
            document.getElementById(
                "secondsInput"
            ).value
        );


    if (
        !Number.isInteger(seconds) ||
        seconds < {{ min_seconds }} ||
        seconds > {{ max_seconds }}
    ) {

        showToast(
            "กรุณากรอกเวลาให้ถูกต้อง",
            "error"
        );

        return;
    }


    try {

        await api(
            "/api/save-config",
            {
                method: "POST",

                body: JSON.stringify({
                    place_id: pid,
                    reason: reason,
                    seconds: seconds
                })
            }
        );


        showToast(
            "💾 บันทึกข้อมูลสำเร็จ",
            "success"
        );

        await loadMapState();

    }
    catch (error) {

        showToast(
            "❌ บันทึกไม่สำเร็จ: " +
            error.message,
            "error"
        );
    }
}


// ============================================================
// SINGLE ACTION
// ============================================================

async function sendAction(
    actionType
) {

    const pid =
        document.getElementById(
            "mapSelect"
        ).value;


    if (pid === "ALL") {

        showToast(
            "กรุณาเลือกแมพ หรือใช้ปุ่มควบคุมทั้งหมด",
            "error"
        );

        return;
    }


    const reason =
        document.getElementById(
            "reasonInput"
        ).value.trim();

    const seconds =
        Number(
            document.getElementById(
                "secondsInput"
            ).value
        );


    if (
        !Number.isInteger(seconds) ||
        seconds < {{ min_seconds }} ||
        seconds > {{ max_seconds }}
    ) {

        showToast(
            "เวลาไม่ถูกต้อง",
            "error"
        );

        return;
    }


    let mode = "open";


    if (
        actionType === "scheduled"
    ) {

        mode =
            document.getElementById(
                "timerEnabled"
            ).checked
            ? "scheduled"
            : "closed";
    }


    if (
        actionType === "open" ||
        actionType === "open-cancel"
    ) {

        mode = "open";
    }


    if (
        actionType === "scheduled" &&
        !confirm(
            `ยืนยันเริ่มระบบปิดใน ${seconds} วินาที?`
        )
    ) {
        return;
    }


    try {

        await api(
            "/api/update",
            {
                method: "POST",

                body: JSON.stringify({
                    place_id: pid,
                    mode: mode,
                    reason: reason,
                    seconds: seconds
                })
            }
        );


        showToast(
            "✅ บันทึกคำสั่งสำเร็จ",
            "success"
        );


        await loadMapState();

    }
    catch (error) {

        showToast(
            "❌ ไม่สามารถบันทึกคำสั่ง: " +
            error.message,
            "error"
        );
    }
}


// ============================================================
// ALL MAPS
// ============================================================

async function sendActionAll(
    actionType
) {

    const reason =
        document.getElementById(
            "reasonInput"
        ).value.trim();

    const seconds =
        Number(
            document.getElementById(
                "secondsInput"
            ).value
        );


    let mode = "open";


    if (
        actionType === "scheduled"
    ) {

        mode =
            document.getElementById(
                "timerEnabled"
            ).checked
            ? "scheduled"
            : "closed";
    }


    const message =
        mode === "scheduled"
        ? `ยืนยันปิดทุกแมพใน ${seconds} วินาที?`
        : `ยืนยันเปลี่ยนสถานะทุกแมพเป็น "${mode}"?`;


    if (!confirm(message)) {
        return;
    }


    try {

        await api(
            "/api/update-all",
            {
                method: "POST",

                body: JSON.stringify({
                    mode: mode,
                    reason: reason,
                    seconds: seconds
                })
            }
        );


        showToast(
            "🌐 ดำเนินการกับทุกแมพสำเร็จ",
            "success"
        );


        await loadMapState();

    }
    catch (error) {

        showToast(
            "❌ การควบคุมทุกแมพล้มเหลว: " +
            error.message,
            "error"
        );
    }
}


// ============================================================
// TOKEN
// ============================================================

async function toggleToken() {

    const pin =
        document.getElementById(
            "pinInput"
        ).value;


    if (!pin) {

        showToast(
            "กรุณากรอก PIN",
            "error"
        );

        return;
    }


    try {

        if (!tokenVisible) {

            const data =
                await api(
                    "/api/token",
                    {
                        method: "POST",

                        body: JSON.stringify({
                            pin: pin
                        })
                    }
                );

            document.getElementById(
                "tokenDisplay"
            ).innerText =
                data.token;

            tokenVisible = true;

        }
        else {

            document.getElementById(
                "tokenDisplay"
            ).innerText =
                "••••••••••••••••••••••••••••••••";

            tokenVisible = false;
        }

    }
    catch (error) {

        showToast(
            "❌ " + error.message,
            "error"
        );
    }
}


// ============================================================
// NEW TOKEN
// ============================================================

async function generateNewToken() {

    const pin =
        document.getElementById(
            "pinInput"
        ).value;


    if (!pin) {

        showToast(
            "กรุณากรอก PIN",
            "error"
        );

        return;
    }


    if (
        !confirm(
            "⚠️ สร้าง Token ใหม่?\n\n" +
            "Token เดิมจะใช้ไม่ได้ทันที"
        )
    ) {
        return;
    }


    try {

        const data =
            await api(
                "/api/new-token",
                {
                    method: "POST",

                    body: JSON.stringify({
                        pin: pin
                    })
                }
            );


        tokenVisible = true;

        document.getElementById(
            "tokenDisplay"
        ).innerText =
            data.token;


        showToast(
            "🔄 สร้าง Token ใหม่สำเร็จ",
            "success"
        );

    }
    catch (error) {

        showToast(
            "❌ สร้าง Token ไม่สำเร็จ: " +
            error.message,
            "error"
        );
    }
}


// ============================================================
// REALTIME COUNTDOWN
// ============================================================

setInterval(() => {

    if (
        !currentState ||
        currentState.mode !== "scheduled" ||
        !currentState.deadline
    ) {
        return;
    }


    const serverNow =
        (
            Date.now()
            + serverOffset
        ) / 1000;


    const remaining =
        Math.max(
            0,
            currentState.deadline
            - serverNow
        );


    const elapsed =
        Math.max(
            0,
            serverNow
            - (
                currentState.deadline
                - currentState.seconds
            )
        );


    const countdown =
        document.getElementById(
            "countdownTime"
        );


    if (countdown) {

        countdown.innerText =
            formatDuration(
                remaining
            );
    }

}, 250);


// ============================================================
// AUTO REFRESH
// ============================================================

setInterval(
    loadMapState,
    3000
);


// ============================================================
// INITIAL
// ============================================================

toggleTimerInput();

loadMapState();

</script>

</body>
</html>
"""


# ============================================================
# ROUTES
# ============================================================

@app.route("/", methods=["GET", "POST"])
def dashboard():

    if request.method == "POST":

        password =
            request.form.get(
                "password",
                ""
            )

        if secrets.compare_digest(
            password,
            ADMIN_PASSWORD
        ):

            session.clear()

            session["admin_authenticated"] = True

            session.permanent = True

            return redirect(
                url_for("dashboard")
            )

        return render_template_string(
            LOGIN_HTML,
            error="รหัสผ่านไม่ถูกต้อง"
        )


    if not is_admin():

        return render_template_string(
            LOGIN_HTML,
            error=None
        )


    return render_template_string(
        HTML_TEMPLATE,

        maps=MAPS,

        default_reason=DEFAULT_REASON,

        default_seconds=DEFAULT_SECONDS,

        min_seconds=MIN_SECONDS,

        max_seconds=MAX_SECONDS,
    )


# ============================================================
# LOGOUT
# ============================================================

@app.get("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# GET STATE
# ============================================================

@app.get("/api/state/<place_id>")
def api_get_state(place_id):

    auth_error = admin_required()

    if auth_error:
        return auth_error


    if place_id not in MAPS.values():

        return jsonify({
            "success": False,
            "error": "unknown place"
        }), 404


    response = jsonify(
        snapshot(place_id)
    )

    response.headers[
        "Cache-Control"
    ] = "no-store, no-cache, must-revalidate"

    return response


# ============================================================
# SAVE CONFIG
# ============================================================

@app.post("/api/save-config")
def api_save_config():

    auth_error = admin_required()

    if auth_error:
        return auth_error


    req = request.get_json(
        silent=True
    ) or {}


    pid = req.get(
        "place_id"
    )

    reason = normalize_reason(
        req.get("reason")
    )

    seconds = safe_int(
        req.get("seconds"),
        DEFAULT_SECONDS
    )


    # --------------------------------------------------------
    # ALL
    # --------------------------------------------------------

    if pid == "ALL":

        errors = []

        for place_id in MAPS.values():

            ok, message = update_state(
                place_id,
                reason=reason,
                seconds=seconds
            )

            if not ok:
                errors.append(
                    f"{place_id}: {message}"
                )


        if errors:

            return jsonify({
                "success": False,
                "error": "บางแมพบันทึกไม่สำเร็จ",
                "details": errors
            }), 503


        return jsonify({
            "success": True,
            "message": "บันทึกทุกแมพสำเร็จ"
        })


    # --------------------------------------------------------
    # SINGLE
    # --------------------------------------------------------

    if pid not in MAPS.values():

        return jsonify({
            "success": False,
            "error": "unknown place"
        }), 404


    ok, message = update_state(
        pid,
        reason=reason,
        seconds=seconds
    )


    if not ok:

        return jsonify({
            "success": False,
            "error": "บันทึก Supabase ไม่สำเร็จ",
            "details": message
        }), 503


    return jsonify({
        "success": True,
        "message": "บันทึกสำเร็จ"
    })


# ============================================================
# UPDATE SINGLE
# ============================================================

@app.post("/api/update")
def api_update():

    auth_error = admin_required()

    if auth_error:
        return auth_error


    req = request.get_json(
        silent=True
    ) or {}


    pid = req.get(
        "place_id"
    )

    mode = req.get(
        "mode"
    )

    reason = normalize_reason(
        req.get("reason")
    )

    seconds = safe_int(
        req.get("seconds"),
        DEFAULT_SECONDS
    )


    if pid not in MAPS.values():

        return jsonify({
            "success": False,
            "error": "unknown place"
        }), 404


    if mode not in {
        "open",
        "closed",
        "scheduled"
    }:

        return jsonify({
            "success": False,
            "error": "invalid mode"
        }), 400


    ok, message = update_state(
        pid,
        mode=mode,
        reason=reason,
        seconds=seconds
    )


    if not ok:

        return jsonify({
            "success": False,
            "error": "บันทึกไม่สำเร็จ",
            "details": message
        }), 503


    return jsonify({
        "success": True,
        "message": "คำสั่งถูกบันทึกแล้ว",
        "state": snapshot(pid)
    })


# ============================================================
# UPDATE ALL
# ============================================================

@app.post("/api/update-all")
def api_update_all():

    auth_error = admin_required()

    if auth_error:
        return auth_error


    req = request.get_json(
        silent=True
    ) or {}


    mode = req.get(
        "mode"
    )

    reason = normalize_reason(
        req.get("reason")
    )

    seconds = safe_int(
        req.get("seconds"),
        DEFAULT_SECONDS
    )


    if mode not in {
        "open",
        "closed",
        "scheduled"
    }:

        return jsonify({
            "success": False,
            "error": "invalid mode"
        }), 400


    errors = []


    with LOCK:

        # ----------------------------------------------------
        # สร้าง state ใหม่ก่อน
        # ----------------------------------------------------

        candidates = {}

        for place_id in MAPS.values():

            old_state = copy.deepcopy(
                DATA["places"][place_id]
            )

            new_state = copy.deepcopy(
                old_state
            )

            new_state["mode"] = mode
            new_state["reason"] = reason
            new_state["seconds"] = seconds


            if mode == "scheduled":

                new_state["deadline"] = (
                    now_ts() + seconds
                )

            else:

                new_state["deadline"] = None


            candidates[place_id] = new_state


        # ----------------------------------------------------
        # Save ทุกแมพ
        # ----------------------------------------------------

        saved = []

        for place_id, state in candidates.items():

            if save_record_to_supabase(
                place_id,
                state
            ):

                saved.append(
                    place_id
                )

            else:

                errors.append(
                    place_id
                )


        # ----------------------------------------------------
        # ถ้ามีแมพใด save ไม่สำเร็จ
        # ----------------------------------------------------

        if errors:

            # พยายาม rollback แมพที่ save ไปแล้ว
            for place_id in saved:

                old_state = DATA["places"].get(
                    place_id
                )

                if old_state:

                    save_record_to_supabase(
                        place_id,
                        old_state
                    )


            return jsonify({
                "success": False,
                "error": "ไม่สามารถบันทึกทุกแมพได้",
                "failed_places": errors
            }), 503


        # ----------------------------------------------------
        # ทุกแมพสำเร็จ → commit memory
        # ----------------------------------------------------

        for place_id, state in candidates.items():

            DATA["places"][place_id] = (
                copy.deepcopy(state)
            )


        save_local_backup()


    return jsonify({
        "success": True,
        "message": "บันทึกทุกแมพสำเร็จ"
    })


# ============================================================
# TOKEN VIEW
# ============================================================

@app.post("/api/token")
def api_token():

    auth_error = admin_required()

    if auth_error:
        return auth_error


    req = request.get_json(
        silent=True
    ) or {}


    pin = str(
        req.get("pin", "")
    )


    if not secrets.compare_digest(
        pin,
        TOKEN_PIN
    ):

        return jsonify({
            "success": False,
            "error": "PIN ไม่ถูกต้อง"
        }), 403


    return jsonify({
        "success": True,
        "token": DATA["token"]
    })


# ============================================================
# NEW TOKEN
# ============================================================

@app.post("/api/new-token")
def api_new_token():

    auth_error = admin_required()

    if auth_error:
        return auth_error


    req = request.get_json(
        silent=True
    ) or {}


    pin = str(
        req.get("pin", "")
    )


    if not secrets.compare_digest(
        pin,
        TOKEN_PIN
    ):

        return jsonify({
            "success": False,
            "error": "PIN ไม่ถูกต้อง"
        }), 403


    with LOCK:

        new_token = secrets.token_urlsafe(
            32
        )


        token_state = {
            "mode": "token",
            "reason": new_token,
            "deadline": None,
            "seconds": DEFAULT_SECONDS,
        }


        if not save_record_to_supabase(
            SYSTEM_TOKEN_ID,
            token_state
        ):

            return jsonify({
                "success": False,
                "error":
                    "ไม่สามารถบันทึก Token ใหม่ลง Supabase"
            }), 503


        DATA["token"] = new_token

        save_local_backup()


        return jsonify({
            "success": True,
            "token": new_token
        })


# ============================================================
# ROBLOX API
# ============================================================

@app.get("/state/<place_id>")
def get_state(place_id):

    expected =
        "Bearer " + DATA["token"]

    supplied =
        request.headers.get(
            "Authorization",
            ""
        )


    if not secrets.compare_digest(
        supplied,
        expected
    ):

        return jsonify({
            "error": "unauthorized"
        }), 401


    if place_id not in MAPS.values():

        return jsonify({
            "error": "unknown place"
        }), 404


    response = jsonify(
        snapshot(place_id)
    )


    response.headers[
        "Cache-Control"
    ] = (
        "no-store, "
        "no-cache, "
        "must-revalidate, "
        "max-age=0"
    )


    return response


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "serverTime": now_ts(),
        "places": len(
            DATA["places"]
        )
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("Roblox Control Center")
    print("=" * 60)

    print(
        f"Server: http://{HOST}:{PORT}"
    )

    print(
        f"Maps: {len(MAPS)}"
    )

    print(
        f"Backup: {BACKUP_FILE}"
    )

    print("=" * 60)

    serve(
        app,
        host=HOST,
        port=PORT,
        threads=8
    )
