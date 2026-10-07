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

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
)
from waitress import serve


# ============================================================
# Roblox Control Center
# Stable + Secure Edition
# ============================================================
#
# Required Environment Variables:
#
# ADMIN_PASSWORD
# TOKEN_PIN
# SECRET_KEY
# SUPABASE_URL
# SUPABASE_KEY
#
# Optional:
#
# PORT=8888
# COOKIE_SECURE=1
# CONTROL_BACKUP_FILE=control_backup.json
#
#
# Supabase table:
#
# control_state
#
# Required columns:
#
# place_id              text PRIMARY KEY / UNIQUE
# mode                  text
# reason                text
# deadline              double precision / numeric / nullable
# seconds               integer / nullable
# command_started_at    double precision / numeric / nullable
#
# ============================================================


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LOG = logging.getLogger("roblox-control-center")


# ============================================================
# Environment
# ============================================================

def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


ADMIN_PASSWORD = env_required("ADMIN_PASSWORD")
TOKEN_PIN = env_required("TOKEN_PIN")
SECRET_KEY = env_required("SECRET_KEY")

SUPABASE_URL = env_required("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = env_required("SUPABASE_KEY")


# ============================================================
# Server
# ============================================================

HOST = "0.0.0.0"

PORT = int(
    os.environ.get(
        "PORT",
        "8888",
    )
)

DEFAULT_REASON = (
    "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง"
)

BACKUP_FILE = os.environ.get(
    "CONTROL_BACKUP_FILE",
    "control_backup.json",
)


# ============================================================
# Roblox Maps
# ============================================================

MAPS = {
    "Place 1": "120651982896178",
    "Place 2": "77210125175879",
}

PLACE_IDS = tuple(MAPS.values())

SYSTEM_TOKEN_ID = "__SYS_TOKEN__"


# ============================================================
# Limits
# ============================================================

MIN_SECONDS = 1
MAX_SECONDS = 86400

MAX_REASON_LENGTH = 500

SUPABASE_TIMEOUT = 8

SAVE_RETRIES = 3

BACKUP_VERSION = 1


# ============================================================
# Locks
# ============================================================

LOCK = threading.RLock()

WRITE_LOCK = threading.Lock()

BACKUP_LOCK = threading.Lock()

INIT_LOCK = threading.Lock()


# ============================================================
# Runtime Status
# ============================================================

DB_AVAILABLE = False

LAST_DB_SYNC = None

LAST_DB_ERROR = None

BACKUP_AVAILABLE = False

LAST_BACKUP_SAVE = None

LAST_BACKUP_ERROR = None

INITIALIZED = False

LAST_INIT_ATTEMPT = 0.0


# Prevent multiple transition-save threads
TRANSITION_PENDING = set()


# ============================================================
# Flask
# ============================================================

app = Flask(__name__)

app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.environ.get(
            "COOKIE_SECURE",
            "1",
        )
        == "1"
    ),
    MAX_CONTENT_LENGTH=1024 * 1024,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


# ============================================================
# Default State
# ============================================================

def default_state():
    return {
        "mode": "open",
        "reason": DEFAULT_REASON,
        "deadline": None,
        "seconds": 60,
        "commandStartedAt": None,
    }


DATA = {
    "token": None,
    "places": {
        pid: default_state()
        for pid in PLACE_IDS
    },
}


# ============================================================
# Cache Control
# ============================================================

@app.after_request
def add_no_cache_headers(response):
    if (
        request.path.startswith("/api/")
        or request.path.startswith("/state/")
    ):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, "
            "max-age=0, private"
        )

        response.headers["Pragma"] = "no-cache"

        response.headers["Expires"] = "0"

    return response


# ============================================================
# Security Helpers
# ============================================================

def safe_equal(a, b):
    return secrets.compare_digest(
        str(a or "").encode("utf-8"),
        str(b or "").encode("utf-8"),
    )


def logged_in():
    return (
        session.get(
            "admin_authenticated"
        )
        is True
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):

        if not logged_in():

            if request.path.startswith("/api/"):
                return jsonify({
                    "ok": False,
                    "error": "unauthorized",
                }), 401

            return redirect("/")

        return view(
            *args,
            **kwargs,
        )

    return wrapped


def verify_token_pin(value):
    return safe_equal(
        value,
        TOKEN_PIN,
    )


# ============================================================
# JSON Helper
# ============================================================

def request_json():
    data = request.get_json(
        silent=True
    )

    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise ValueError(
            "ข้อมูล JSON ไม่ถูกต้อง"
        )

    return data


# ============================================================
# Normalization
# ============================================================

def normalize_seconds(value, default=60):
    if value is None:
        value = default

    try:
        number = int(value)

    except (
        TypeError,
        ValueError,
    ):
        raise ValueError(
            "เวลา Countdown ต้องเป็นตัวเลขจำนวนเต็ม"
        )

    if number < MIN_SECONDS:
        raise ValueError(
            f"เวลาต้องไม่น้อยกว่า {MIN_SECONDS} วินาที"
        )

    if number > MAX_SECONDS:
        raise ValueError(
            f"เวลาต้องไม่เกิน {MAX_SECONDS} วินาที"
        )

    return number


def normalize_reason(value):
    if value is None:
        return DEFAULT_REASON

    reason = str(value).strip()

    if not reason:
        return DEFAULT_REASON

    return reason[:MAX_REASON_LENGTH]


def normalize_timestamp(value):
    if value is None:
        return None

    try:
        return float(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


def clean_state(state):
    if not isinstance(state, dict):
        state = default_state()

    mode = state.get(
        "mode",
        "open",
    )

    if mode not in {
        "open",
        "scheduled",
        "closed",
    }:
        mode = "open"

    return {
        "mode": mode,
        "reason": normalize_reason(
            state.get(
                "reason",
                DEFAULT_REASON,
            )
        ),
        "deadline": normalize_timestamp(
            state.get("deadline")
        ),
        "seconds": normalize_seconds(
            state.get(
                "seconds",
                60,
            )
        ),
        "commandStartedAt": normalize_timestamp(
            state.get(
                "commandStartedAt"
            )
        ),
    }


# ============================================================
# Supabase Headers
# ============================================================

def supabase_headers(prefer=None):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": (
            f"Bearer {SUPABASE_KEY}"
        ),
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    return headers


# ============================================================
# Supabase Request
# ============================================================

def supabase_request(
    method,
    path,
    payload=None,
    query=None,
    timeout=SUPABASE_TIMEOUT,
    prefer=None,
):
    url = (
        f"{SUPABASE_URL}/rest/v1/"
        f"{path.lstrip('/')}"
    )

    if query:
        url += "?" + urllib.parse.urlencode(
            query,
            doseq=True,
        )

    body = None

    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers=supabase_headers(
            prefer
        ),
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw:
                return None

            try:
                return json.loads(raw)

            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:

        try:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )

        except Exception:
            detail = str(exc)

        raise RuntimeError(
            f"Supabase HTTP {exc.code}: "
            f"{detail[:800]}"
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:

        raise RuntimeError(
            f"Supabase connection failed: {exc}"
        ) from exc


# ============================================================
# Load From Supabase
# ============================================================

def load_from_supabase():
    global DB_AVAILABLE
    global LAST_DB_SYNC
    global LAST_DB_ERROR

    rows = supabase_request(
        "GET",
        "control_state",
        query={
            "select": (
                "place_id,"
                "mode,"
                "reason,"
                "deadline,"
                "seconds,"
                "command_started_at"
            ),
        },
    )

    if not isinstance(rows, list):
        raise RuntimeError(
            "Supabase returned unexpected response."
        )

    result = {
        "token": None,
        "places": {},
        "missing": [],
    }

    found = set()

    for row in rows:

        if not isinstance(row, dict):
            continue

        place_id = str(
            row.get(
                "place_id",
                "",
            )
        )

        # ----------------------------------------------------
        # System Token
        # ----------------------------------------------------

        if place_id == SYSTEM_TOKEN_ID:

            token = row.get("reason")

            if token:
                result["token"] = str(token)

            continue

        # ----------------------------------------------------
        # Ignore unknown place
        # ----------------------------------------------------

        if place_id not in PLACE_IDS:
            continue

        found.add(place_id)

        mode = row.get(
            "mode",
            "open",
        )

        if mode not in {
            "open",
            "scheduled",
            "closed",
        }:
            mode = "open"

        reason = normalize_reason(
            row.get("reason")
        )

        try:
            seconds = normalize_seconds(
                row.get(
                    "seconds",
                    60,
                )
            )

        except ValueError:
            seconds = 60

        deadline = normalize_timestamp(
            row.get("deadline")
        )

        command_started_at = normalize_timestamp(
            row.get(
                "command_started_at"
            )
        )

        result["places"][place_id] = {
            "mode": mode,
            "reason": reason,
            "deadline": deadline,
            "seconds": seconds,
            "commandStartedAt": (
                command_started_at
            ),
        }

    result["missing"] = [
        pid
        for pid in PLACE_IDS
        if pid not in found
    ]

    for pid in PLACE_IDS:

        result["places"].setdefault(
            pid,
            default_state(),
        )

    DB_AVAILABLE = True

    LAST_DB_SYNC = time.time()

    LAST_DB_ERROR = None

    return result


# ============================================================
# Save To Supabase
# ============================================================

def save_to_supabase(
    place_id,
    state_data,
):
    """
    บันทึกข้อมูลลง Supabase และตรวจสอบว่า
    Supabase รับค่าที่เราส่งจริง

    IMPORTANT:
    - ไม่เปลี่ยน DATA โดยตรง
    - ไม่เปลี่ยนโครงสร้าง state
    - ใช้ return=representation
      เพื่อยืนยันข้อมูลที่ถูกบันทึก
    """

    if place_id == SYSTEM_TOKEN_ID:

        payload = {
            "place_id": SYSTEM_TOKEN_ID,
            "mode": "token",
            "reason": str(
                state_data["token"]
            ),
            "deadline": None,
            "seconds": 0,
            "command_started_at": None,
        }

    else:

        state_data = clean_state(
            state_data
        )

        payload = {
            "place_id": place_id,
            "mode": state_data["mode"],
            "reason": state_data["reason"],
            "deadline": state_data["deadline"],
            "seconds": state_data["seconds"],
            "command_started_at": (
                state_data[
                    "commandStartedAt"
                ]
            ),
        }

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # เดิมใช้ return=minimal
    #
    # ตอนนี้ใช้ return=representation
    # เพื่อให้ Supabase ส่ง row ที่บันทึกกลับมา
    # --------------------------------------------------------

    result = supabase_request(
        "POST",
        "control_state",
        payload=payload,
        query={
            "on_conflict": "place_id",
        },
        prefer=(
            "resolution=merge-duplicates,"
            "return=representation"
        ),
    )

    # --------------------------------------------------------
    # Verify Token
    # --------------------------------------------------------

    if place_id == SYSTEM_TOKEN_ID:

        if not isinstance(
            result,
            list,
        ) or not result:

            raise RuntimeError(
                "Supabase ไม่ยืนยันการบันทึก Token"
            )

        saved_row = result[0]

        saved_token = str(
            saved_row.get(
                "reason",
                "",
            )
        )

        expected_token = str(
            payload["reason"]
        )

        if not safe_equal(
            saved_token,
            expected_token,
        ):

            raise RuntimeError(
                "Supabase บันทึก Token "
                "แต่ค่าที่อ่านกลับมาไม่ตรงกับค่าที่ส่ง"
            )

        return result

    # --------------------------------------------------------
    # Verify Normal Place
    # --------------------------------------------------------

    if not isinstance(
        result,
        list,
    ) or not result:

        raise RuntimeError(
            f"Supabase ไม่ยืนยันการบันทึก Place {place_id}"
        )

    saved_row = result[0]

    saved_place_id = str(
        saved_row.get(
            "place_id",
            "",
        )
    )

    if saved_place_id != place_id:

        raise RuntimeError(
            f"Supabase บันทึกผิด Place ID: "
            f"{saved_place_id}"
        )

    # --------------------------------------------------------
    # Verify Reason
    # --------------------------------------------------------

    saved_reason = normalize_reason(
        saved_row.get(
            "reason"
        )
    )

    expected_reason = normalize_reason(
        payload["reason"]
    )

    if saved_reason != expected_reason:

        raise RuntimeError(
            "Supabase บันทึกเหตุผล "
            "แต่ค่าที่อ่านกลับมาไม่ตรงกับค่าที่ส่ง"
        )

    # --------------------------------------------------------
    # Verify Seconds
    # --------------------------------------------------------

    try:

        saved_seconds = normalize_seconds(
            saved_row.get(
                "seconds"
            )
        )

    except ValueError:

        raise RuntimeError(
            "Supabase บันทึกเวลา "
            "แต่ค่าที่อ่านกลับมาไม่ถูกต้อง"
        )

    if saved_seconds != int(
        payload["seconds"]
    ):

        raise RuntimeError(
            "Supabase บันทึกเวลา "
            "แต่ค่าที่อ่านกลับมาไม่ตรงกับค่าที่ส่ง"
        )

    # --------------------------------------------------------
    # Verify Mode
    # --------------------------------------------------------

    saved_mode = saved_row.get(
        "mode",
        "open",
    )

    if saved_mode != payload["mode"]:

        raise RuntimeError(
            "Supabase บันทึกสถานะ "
            "แต่ค่าที่อ่านกลับมาไม่ตรงกับค่าที่ส่ง"
        )

    return result


# ============================================================
# Supabase Retry
# ============================================================

def save_with_retry(
    place_id,
    state_data,
    retries=SAVE_RETRIES,
):
    global DB_AVAILABLE
    global LAST_DB_SYNC
    global LAST_DB_ERROR

    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):

        try:

            save_to_supabase(
                place_id,
                state_data,
            )

            DB_AVAILABLE = True

            LAST_DB_SYNC = time.time()

            LAST_DB_ERROR = None

            return True, None

        except Exception as exc:

            last_error = str(exc)

            LAST_DB_ERROR = last_error

            LOG.warning(
                "Supabase save failed "
                "(%s/%s) for %s: %s",
                attempt,
                retries,
                place_id,
                last_error,
            )

            if attempt < retries:

                time.sleep(
                    0.25 * attempt
                )

    DB_AVAILABLE = False

    return False, last_error


# ============================================================
# Local Backup
# ============================================================

def backup_payload():
    with LOCK:
        return {
            "version": BACKUP_VERSION,
            "savedAt": time.time(),
            "token": DATA.get("token"),
            "places": copy.deepcopy(
                DATA.get(
                    "places",
                    {},
                )
            ),
        }


def save_local_backup():
    global BACKUP_AVAILABLE
    global LAST_BACKUP_SAVE
    global LAST_BACKUP_ERROR

    temp_file = (
        BACKUP_FILE + ".tmp"
    )

    try:

        payload = backup_payload()

        directory = os.path.dirname(
            os.path.abspath(
                BACKUP_FILE
            )
        )

        os.makedirs(
            directory,
            exist_ok=True,
        )

        with BACKUP_LOCK:

            with open(
                temp_file,
                "w",
                encoding="utf-8",
            ) as file:

                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )

                file.flush()

                os.fsync(
                    file.fileno()
                )

            os.replace(
                temp_file,
                BACKUP_FILE,
            )

        # Best effort restrictive permissions
        if os.name != "nt":

            try:
                os.chmod(
                    BACKUP_FILE,
                    0o600,
                )

            except OSError:
                pass

        BACKUP_AVAILABLE = True

        LAST_BACKUP_SAVE = time.time()

        LAST_BACKUP_ERROR = None

        return True, None

    except Exception as exc:

        BACKUP_AVAILABLE = False

        LAST_BACKUP_ERROR = str(exc)

        LOG.error(
            "Local backup failed: %s",
            exc,
        )

        try:
            if os.path.exists(
                temp_file
            ):
                os.remove(
                    temp_file
                )

        except OSError:
            pass

        return False, str(exc)


def load_local_backup():
    global BACKUP_AVAILABLE
    global LAST_BACKUP_ERROR

    if not os.path.exists(
        BACKUP_FILE
    ):

        BACKUP_AVAILABLE = False

        return None

    try:

        with BACKUP_LOCK:

            with open(
                BACKUP_FILE,
                "r",
                encoding="utf-8",
            ) as file:

                payload = json.load(
                    file
                )

        if not isinstance(
            payload,
            dict,
        ):
            raise RuntimeError(
                "Backup format invalid."
            )

        token = payload.get(
            "token"
        )

        places = payload.get(
            "places",
            {},
        )

        if not isinstance(
            places,
            dict,
        ):
            raise RuntimeError(
                "Backup places invalid."
            )

        clean_places = {}

        for pid in PLACE_IDS:

            raw = places.get(pid)

            if not isinstance(
                raw,
                dict,
            ):
                raw = default_state()

            clean_places[pid] = clean_state(
                raw
            )

        BACKUP_AVAILABLE = True

        LAST_BACKUP_ERROR = None

        return {
            "token": (
                str(token)
                if token
                else None
            ),
            "places": clean_places,
        }

    except Exception as exc:

        BACKUP_AVAILABLE = False

        LAST_BACKUP_ERROR = str(exc)

        LOG.error(
            "Local backup load failed: %s",
            exc,
        )

        return None


# ============================================================
# Initialize Database
# ============================================================

def initialize_database():
    global INITIALIZED
    global DB_AVAILABLE

    try:

        loaded = load_from_supabase()

    except Exception as exc:

        DB_AVAILABLE = False

        LOG.error(
            "Supabase initial load failed: %s",
            exc,
        )

        backup = load_local_backup()

        if backup:

            with LOCK:

                DATA["places"].update(
                    copy.deepcopy(
                        backup["places"]
                    )
                )

                if backup.get("token"):
                    DATA["token"] = (
                        backup["token"]
                    )

            LOG.warning(
                "Running from local backup."
            )

        return False

    # --------------------------------------------------------
    # Publish DB data
    # --------------------------------------------------------

    with LOCK:

        DATA["places"].update(
            copy.deepcopy(
                loaded["places"]
            )
        )

        if loaded.get("token"):

            DATA["token"] = (
                loaded["token"]
            )

    # --------------------------------------------------------
    # Create Token if missing
    # --------------------------------------------------------

    if not DATA.get("token"):

        new_token = secrets.token_urlsafe(
            32
        )

        ok, error = save_with_retry(
            SYSTEM_TOKEN_ID,
            {
                "token": new_token
            },
        )

        if not ok:

            LOG.error(
                "Could not save initial token: %s",
                error,
            )

            return False

        with LOCK:
            DATA["token"] = new_token

    # --------------------------------------------------------
    # Create missing places
    # --------------------------------------------------------

    for pid in loaded.get(
        "missing",
        [],
    ):

        state = default_state()

        ok, error = save_with_retry(
            pid,
            state,
        )

        if not ok:

            LOG.error(
                "Could not initialize %s: %s",
                pid,
                error,
            )

            continue

        with LOCK:

            DATA["places"][pid] = (
                copy.deepcopy(
                    state
                )
            )

    # --------------------------------------------------------
    # Save known-good backup
    # --------------------------------------------------------

    save_local_backup()

    INITIALIZED = True

    LOG.info(
        "Database initialization complete."
    )

    return True


def ensure_db_loaded():
    global LAST_INIT_ATTEMPT

    if INITIALIZED:
        return

    now = time.time()

    if (
        now - LAST_INIT_ATTEMPT
        < 5
    ):
        return

    if not INIT_LOCK.acquire(
        blocking=False
    ):
        return

    try:

        if INITIALIZED:
            return

        LAST_INIT_ATTEMPT = now

        initialize_database()

    finally:

        INIT_LOCK.release()


# ============================================================
# Build Expired View
# ============================================================

def build_expired_state(
    state,
):
    """
    Creates a CLOSED view when a scheduled timer expires.

    IMPORTANT:
    This function DOES NOT modify DATA.
    """

    view = copy.deepcopy(
        state
    )

    if (
        view["mode"]
        == "scheduled"
        and view["deadline"]
        is not None
        and time.time()
        >= float(
            view["deadline"]
        )
    ):

        view["mode"] = "closed"

    return view


# ============================================================
# Persist Scheduled -> Closed
# ============================================================

def persist_transition(
    place_id,
    expected_deadline,
):
    try:

        with WRITE_LOCK:

            with LOCK:

                current = copy.deepcopy(
                    DATA["places"][place_id]
                )

            # Someone may have changed the map
            # while the background task was waiting.
            if (
                current["mode"]
                != "scheduled"
                or current["deadline"]
                != expected_deadline
            ):
                return

            new_state = copy.deepcopy(
                current
            )

            new_state["mode"] = "closed"

            # Keep deadline as the actual close time.
            # This preserves historical timing.
            new_state["deadline"] = (
                expected_deadline
            )

            ok, error = save_with_retry(
                place_id,
                new_state,
                retries=2,
            )

            if not ok:

                LOG.error(
                    "Could not persist "
                    "scheduled->closed for %s: %s",
                    place_id,
                    error,
                )

                return

            # ONLY after Supabase succeeded
            # publish the new state.
            with LOCK:

                current_after = DATA[
                    "places"
                ][place_id]

                if (
                    current_after["mode"]
                    == "scheduled"
                    and current_after[
                        "deadline"
                    ]
                    == expected_deadline
                ):

                    DATA[
                        "places"
                    ][place_id] = (
                        copy.deepcopy(
                            new_state
                        )
                    )

            save_local_backup()

    finally:

        with LOCK:
            TRANSITION_PENDING.discard(
                place_id
            )


def request_transition_if_needed(
    place_id,
    state,
):
    if (
        state["mode"]
        != "scheduled"
        or state["deadline"]
        is None
    ):
        return

    if (
        time.time()
        < float(
            state["deadline"]
        )
    ):
        return

    with LOCK:

        if place_id in TRANSITION_PENDING:
            return

        TRANSITION_PENDING.add(
            place_id
        )

        expected_deadline = (
            state["deadline"]
        )

    threading.Thread(
        target=persist_transition,
        args=(
            place_id,
            expected_deadline,
        ),
        daemon=True,
    ).start()


# ============================================================
# Snapshot
# ============================================================

def snapshot(place_id):
    ensure_db_loaded()

    if place_id not in PLACE_IDS:
        raise ValueError(
            "unknown place"
        )

    with LOCK:

        base_state = copy.deepcopy(
            DATA["places"][place_id]
        )

    # --------------------------------------------------------
    # Do NOT mutate DATA here.
    # --------------------------------------------------------

    view_state = build_expired_state(
        base_state
    )

    if (
        base_state["mode"]
        == "scheduled"
        and base_state["deadline"]
        is not None
        and time.time()
        >= float(
            base_state["deadline"]
        )
    ):

        request_transition_if_needed(
            place_id,
            base_state,
        )

    now = time.time()

    state = view_state

    state["serverNow"] = now

    state["dbAvailable"] = (
        DB_AVAILABLE
    )

    state["backupAvailable"] = (
        BACKUP_AVAILABLE
    )

    state["lastDbSync"] = (
        LAST_DB_SYNC
    )

    state["lastDbError"] = (
        LAST_DB_ERROR
    )

    state["lastBackupSave"] = (
        LAST_BACKUP_SAVE
    )

    state["lastBackupError"] = (
        LAST_BACKUP_ERROR
    )

    # --------------------------------------------------------
    # Scheduled
    # --------------------------------------------------------

    if (
        state["mode"]
        == "scheduled"
        and state["deadline"]
        is not None
    ):

        deadline = float(
            state["deadline"]
        )

        started = (
            state.get(
                "commandStartedAt"
            )
        )

        if started is None:

            started = (
                deadline
                - state["seconds"]
            )

        state["startedAt"] = started

        state["remaining"] = max(
            0,
            deadline - now,
        )

        state["elapsed"] = min(
            state["seconds"],
            max(
                0,
                now - started,
            ),
        )

    # --------------------------------------------------------
    # Closed
    # --------------------------------------------------------

    elif state["mode"] == "closed":

        closed_at = state.get(
            "deadline"
        )

        state["closedAt"] = (
            closed_at
        )

        if closed_at is not None:

            state["closedElapsed"] = max(
                0,
                now - float(
                    closed_at
                ),
            )

        else:

            state["closedElapsed"] = 0

        state["remaining"] = 0

        state["elapsed"] = state[
            "seconds"
        ]

    # --------------------------------------------------------
    # Open
    # --------------------------------------------------------

    else:

        state["remaining"] = 0

        state["elapsed"] = 0

    return state


# ============================================================
# Build New State
# ============================================================

def make_new_state(
    current,
    mode=None,
    reason=None,
    seconds=None,
):
    new_state = copy.deepcopy(
        current
    )

    if reason is not None:

        new_state["reason"] = (
            normalize_reason(
                reason
            )
        )

    if seconds is not None:

        new_state["seconds"] = (
            normalize_seconds(
                seconds
            )
        )

    if mode is not None:

        if mode not in {
            "open",
            "scheduled",
            "closed",
        }:
            raise ValueError(
                "สถานะไม่ถูกต้อง"
            )

        now = time.time()

        new_state["mode"] = mode

        if mode == "scheduled":

            duration = normalize_seconds(
                seconds
                if seconds is not None
                else new_state[
                    "seconds"
                ]
            )

            new_state["seconds"] = (
                duration
            )

            new_state[
                "commandStartedAt"
            ] = now

            new_state["deadline"] = (
                now + duration
            )

        elif mode == "closed":

            new_state["deadline"] = now

            new_state[
                "commandStartedAt"
            ] = (
                new_state.get(
                    "commandStartedAt"
                )
                or now
            )

        elif mode == "open":

            new_state["deadline"] = None

            new_state[
                "commandStartedAt"
            ] = None

    return clean_state(
        new_state
    )


# ============================================================
# Update Individual State
# ============================================================

def update_state(
    place_id,
    mode=None,
    reason=None,
    seconds=None,
):
    if place_id not in PLACE_IDS:

        raise ValueError(
            "ไม่พบ Place ID นี้"
        )

    with WRITE_LOCK:

        with LOCK:

            current = copy.deepcopy(
                DATA["places"][place_id]
            )

        new_state = make_new_state(
            current,
            mode=mode,
            reason=reason,
            seconds=seconds,
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # Save FIRST.
        # DATA remains untouched.
        # ----------------------------------------------------

        ok, error = save_with_retry(
            place_id,
            new_state,
        )

        if not ok:

            raise RuntimeError(
                "บันทึก Supabase ไม่สำเร็จ: "
                + str(error)
            )

        # ----------------------------------------------------
        # Supabase succeeded.
        # Now update RAM.
        # ----------------------------------------------------

        with LOCK:

            DATA["places"][place_id] = (
                copy.deepcopy(
                    new_state
                )
            )

        # ----------------------------------------------------
        # Backup after DB success
        # ----------------------------------------------------

        save_local_backup()

    return snapshot(
        place_id
    )


# ============================================================
# Update All
# ============================================================

def update_all(
    mode,
    reason=None,
    seconds=None,
):
    if mode not in {
        "open",
        "scheduled",
        "closed",
    }:

        raise ValueError(
            "สถานะไม่ถูกต้อง"
        )

    if seconds is not None:

        seconds = normalize_seconds(
            seconds
        )

    reason = normalize_reason(
        reason
    )

    results = []

    for pid in PLACE_IDS:

        try:

            state = update_state(
                pid,
                mode=mode,
                reason=reason,
                seconds=seconds,
            )

            results.append({
                "placeId": pid,
                "ok": True,
                "state": state,
            })

        except Exception as exc:

            LOG.error(
                "Update all failed for %s: %s",
                pid,
                exc,
            )

            results.append({
                "placeId": pid,
                "ok": False,
                "error": str(exc),
            })

    failed = [
        item
        for item in results
        if not item["ok"]
    ]

    return {
        "results": results,
        "success": len(failed) == 0,
        "failed": len(failed),
    }


# ============================================================
# Login HTML
# ============================================================

LOGIN_HTML = r"""
<!doctype html>

<html lang="th">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Roblox Control Center</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    min-height:100vh;
    display:grid;
    place-items:center;
    padding:20px;
    color:#eef2ff;
    background:
        radial-gradient(
            circle at 20% 10%,
            rgba(99,102,241,.20),
            transparent 30%
        ),
        radial-gradient(
            circle at 80% 90%,
            rgba(14,165,233,.14),
            transparent 30%
        ),
        #070a12;
    font-family:
        Inter,
        Segoe UI,
        Arial,
        sans-serif;
}

.card{
    width:min(430px,100%);
    padding:32px;
    border:1px solid #252b3d;
    border-radius:24px;
    background:rgba(15,19,31,.94);
    box-shadow:
        0 24px 80px rgba(0,0,0,.45);
}

.logo{
    width:58px;
    height:58px;
    display:grid;
    place-items:center;
    border-radius:17px;
    background:
        linear-gradient(
            135deg,
            #6366f1,
            #06b6d4
        );
    font-size:27px;
    margin-bottom:20px;
}

h1{
    margin:0 0 8px;
    font-size:25px;
}

p{
    color:#8e99ad;
    margin:0 0 24px;
}

label{
    display:block;
    font-size:13px;
    color:#aeb8ca;
    margin-bottom:8px;
}

input{
    width:100%;
    height:48px;
    padding:0 14px;
    color:#fff;
    background:#0b0f19;
    border:1px solid #293147;
    border-radius:12px;
    outline:none;
}

input:focus{
    border-color:#6366f1;
    box-shadow:
        0 0 0 3px
        rgba(99,102,241,.15);
}

button{
    width:100%;
    height:48px;
    margin-top:14px;
    border:0;
    border-radius:12px;
    color:#fff;
    font-weight:700;
    cursor:pointer;
    background:
        linear-gradient(
            135deg,
            #6366f1,
            #4f46e5
        );
}

.error{
    padding:11px 13px;
    margin-bottom:15px;
    border-radius:10px;
    background:#3a1218;
    color:#ffb4be;
    border:1px solid #69202c;
    font-size:13px;
}

.small{
    margin-top:18px;
    text-align:center;
    font-size:12px;
    color:#657086;
}

</style>

</head>

<body>

<div class="card">

<div class="logo">
🛡️
</div>

<h1>
Roblox Control Center
</h1>

<p>
เข้าสู่ระบบผู้ดูแลระบบ
</p>

{% if error %}

<div class="error">
{{ error }}
</div>

{% endif %}

<form method="post">

<label>
Admin Password
</label>

<input
    type="password"
    name="password"
    autocomplete="current-password"
    placeholder="กรอกรหัสผ่าน"
    required
    autofocus
>

<button type="submit">
เข้าสู่ระบบ →
</button>

</form>

<div class="small">
Secure administrator dashboard
</div>

</div>

</body>

</html>
"""


# ============================================================
# Dashboard HTML
# ============================================================

DASHBOARD_HTML = r"""
<!doctype html>

<html lang="th">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
Roblox Control Center
</title>

<style>

:root{
    --bg:#070a12;
    --panel:#0f1420;
    --border:#242c3e;
    --text:#eef2ff;
    --muted:#8994a8;
    --blue:#6366f1;
    --cyan:#06b6d4;
    --green:#22c55e;
    --yellow:#f59e0b;
    --red:#ef4444;
}

*{
    box-sizing:border-box;
}

body{
    margin:0;
    color:var(--text);
    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(99,102,241,.13),
            transparent 28%
        ),
        radial-gradient(
            circle at 100% 100%,
            rgba(6,182,212,.08),
            transparent 30%
        ),
        var(--bg);
    font-family:
        Inter,
        Segoe UI,
        Arial,
        sans-serif;
}

button,
input,
select{
    font:inherit;
}

.app{
    width:min(1200px,100%);
    margin:auto;
    padding:22px;
}

.header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:15px;
    padding:20px 22px;
    margin-bottom:18px;
    border:1px solid var(--border);
    border-radius:20px;
    background:rgba(15,20,32,.88);
    box-shadow:
        0 20px 60px rgba(0,0,0,.22);
}

.brand{
    display:flex;
    align-items:center;
    gap:13px;
}

.logo{
    width:48px;
    height:48px;
    border-radius:15px;
    display:grid;
    place-items:center;
    font-size:23px;
    background:
        linear-gradient(
            135deg,
            var(--blue),
            var(--cyan)
        );
}

h1{
    font-size:19px;
    margin:0;
}

.sub{
    font-size:12px;
    color:var(--muted);
    margin-top:3px;
}

.header-right{
    display:flex;
    align-items:center;
    gap:9px;
}

.logout{
    text-decoration:none;
    color:#ffc1c1;
    border:1px solid #51222a;
    background:#211016;
    padding:9px 13px;
    border-radius:10px;
    font-size:12px;
}

.grid{
    display:grid;
    grid-template-columns:1.05fr .95fr;
    gap:18px;
}

.panel{
    background:rgba(15,20,32,.90);
    border:1px solid var(--border);
    border-radius:20px;
    padding:20px;
}

.panel h2{
    font-size:15px;
    margin:0 0 17px;
}

.field{
    margin-bottom:15px;
}

label{
    display:block;
    margin-bottom:7px;
    color:#aab4c7;
    font-size:12px;
    font-weight:700;
}

input[type=text],
input[type=number],
input[type=password],
select{
    width:100%;
    height:44px;
    padding:0 12px;
    color:#f8fafc;
    background:#090d16;
    border:1px solid #273047;
    border-radius:10px;
    outline:none;
}

input:focus,
select:focus{
    border-color:var(--blue);
    box-shadow:
        0 0 0 3px
        rgba(99,102,241,.12);
}

.row{
    display:grid;
    grid-template-columns:1fr 220px;
    gap:12px;
}

.check{
    display:flex;
    align-items:center;
    gap:9px;
    padding:11px 12px;
    border:1px solid var(--border);
    border-radius:10px;
    background:#0b1019;
    height:44px;
}

.check input{
    width:17px;
    height:17px;
}

.check label{
    margin:0;
    color:#dce3f1;
}

.buttons{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:9px;
}

.btn{
    min-height:44px;
    padding:10px 12px;
    border:0;
    border-radius:10px;
    color:#fff;
    font-weight:750;
    cursor:pointer;
    transition:.15s;
}

.btn:hover{
    transform:translateY(-1px);
    filter:brightness(1.08);
}

.btn:disabled{
    opacity:.5;
    cursor:not-allowed;
    transform:none;
}

.close{
    background:#b91c1c;
}

.cancel{
    background:#b45309;
}

.open{
    background:#15803d;
}

.blue{
    background:#4338ca;
}

.gray{
    background:#1f2937;
}

.all{
    grid-column:1/-1;
}

.status{
    position:relative;
    overflow:hidden;
    border:1px solid var(--border);
    border-radius:16px;
    padding:17px;
    background:#090e18;
    margin-bottom:12px;
}

.status::before{
    content:"";
    position:absolute;
    left:0;
    top:0;
    bottom:0;
    width:4px;
    background:var(--green);
}

.status.scheduled::before{
    background:var(--yellow);
}

.status.closed::before{
    background:var(--red);
}

.status-head{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:10px;
}

.badge{
    display:inline-flex;
    align-items:center;
    gap:6px;
    padding:5px 8px;
    border-radius:999px;
    font-size:11px;
    font-weight:800;
    background:#102719;
    color:#86efac;
}

.badge.scheduled{
    background:#2b210c;
    color:#fcd34d;
}

.badge.closed{
    background:#2a1115;
    color:#fca5a5;
}

.big{
    margin:12px 0 3px;
    font-size:14px;
    color:#93a4bd;
}

.timer{
    font-variant-numeric:tabular-nums;
    font-size:34px;
    font-weight:900;
    letter-spacing:1px;
}

.progress{
    height:7px;
    background:#1c2535;
    border-radius:99px;
    overflow:hidden;
    margin:12px 0;
}

.progress > div{
    height:100%;
    width:0;
    background:
        linear-gradient(
            90deg,
            var(--blue),
            var(--cyan)
        );
}

.details{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px;
    margin-top:13px;
}

.detail{
    padding:9px 10px;
    border-radius:9px;
    background:#0d1320;
    border:1px solid #1d2638;
}

.detail b{
    display:block;
    color:#68758b;
    font-size:10px;
    margin-bottom:3px;
}

.detail span{
    font-size:12px;
    color:#dbe3f0;
    word-break:break-word;
}

.reason{
    margin-top:10px;
    padding:10px;
    border-radius:9px;
    background:#0d1320;
    color:#bac5d7;
    font-size:12px;
}

.token{
    margin-top:18px;
    padding:15px;
    border:1px solid var(--border);
    border-radius:15px;
    background:#090e18;
}

.token-value{
    margin-top:8px;
    padding:10px;
    background:#060912;
    border-radius:9px;
    color:#67e8f9;
    font-family:
        ui-monospace,
        Consolas,
        monospace;
    font-size:12px;
    word-break:break-all;
    min-height:39px;
}

.token-actions{
    display:grid;
    grid-template-columns:1fr auto auto;
    gap:8px;
    margin-top:9px;
}

.token-actions .btn{
    margin:0;
}

.system-box{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px;
    margin-bottom:14px;
}

.system{
    padding:10px;
    border:1px solid #1d2638;
    border-radius:10px;
    background:#0b1019;
}

.system-title{
    font-size:10px;
    color:#68758b;
    margin-bottom:4px;
}

.system-value{
    font-size:12px;
    font-weight:800;
}

.ok{
    color:#86efac;
}

.off{
    color:#fca5a5;
}

.notice{
    position:fixed;
    right:18px;
    bottom:18px;
    max-width:420px;
    padding:13px 15px;
    border:1px solid #2b3850;
    border-radius:12px;
    background:#111827;
    color:#e5e7eb;
    box-shadow:
        0 15px 50px rgba(0,0,0,.4);
    display:none;
    z-index:100;
}

.notice.show{
    display:block;
}

.notice.ok{
    border-color:#185b35;
}

.notice.bad{
    border-color:#6b2630;
}

@media(max-width:850px){

    .grid{
        grid-template-columns:1fr;
    }

}

@media(max-width:600px){

    .app{
        padding:12px;
    }

    .header{
        padding:15px;
        align-items:flex-start;
    }

    .header-right{
        flex-direction:column;
        align-items:flex-end;
    }

    .buttons{
        grid-template-columns:1fr;
    }

    .row{
        grid-template-columns:1fr;
    }

    .details{
        grid-template-columns:1fr;
    }

    .token-actions{
        grid-template-columns:1fr;
    }

    .system-box{
        grid-template-columns:1fr;
    }

}

</style>

</head>

<body>

<div class="app">

<header class="header">

<div class="brand">

<div class="logo">
🛡️
</div>

<div>

<h1>
Roblox Control Center
</h1>

<div class="sub">
ระบบควบคุมสถานะ Roblox แบบ Real-time
</div>

</div>

</div>

<div class="header-right">

<span
    id="dbBadge"
    class="badge"
>
● กำลังตรวจสอบ
</span>

<a
    class="logout"
    href="/logout"
>
ออกจากระบบ
</a>

</div>

</header>


<div class="grid">


<section class="panel">

<h2>
⚙️ การควบคุม
</h2>


<div class="field">

<label>
เลือกแมพ
</label>

<select
    id="mapSelect"
    onchange="loadSelected()"
>

<option value="ALL">
🌐 จัดการทุกแมพ
</option>

{% for name, pid in maps.items() %}

<option value="{{ pid }}">
{{ name }} · {{ pid }}
</option>

{% endfor %}

</select>

</div>


<div class="field">

<label>
เหตุผล / ข้อความแจ้งเตือน
</label>

<input
    id="reasonInput"
    type="text"
    maxlength="500"
    value="{{ default_reason }}"
>

</div>


<div class="row">

<div class="field">

<label>
เวลา Countdown (วินาที)
</label>

<input
    id="secondsInput"
    type="number"
    min="1"
    max="86400"
    value="60"
>

</div>


<div class="field">

<label>
Timer
</label>

<div class="check">

<input
    id="timerEnabled"
    type="checkbox"
    checked
>

<label for="timerEnabled">
นับถอยหลังก่อนปิด
</label>

</div>

</div>

</div>


<div class="buttons">

<button
    class="btn close"
    onclick="actionSelected('scheduled')"
>
🛑 เริ่มปิด
</button>

<button
    class="btn cancel"
    onclick="actionSelected('open')"
>
↩ ยกเลิก / เปิด
</button>

<button
    class="btn open"
    onclick="actionSelected('open')"
>
🟢 เปิดแมพ
</button>

<button
    class="btn close all"
    onclick="actionAll('closed')"
>
🚨 ปิดทุกแมพทันที
</button>

<button
    class="btn cancel all"
    onclick="actionAll('open')"
>
↩️ เปิด / ยกเลิกทุกแมพ
</button>

<button
    class="btn blue all"
    onclick="actionAll('scheduled')"
>
⏳ Countdown ทุกแมพ
</button>

</div>


<div style="margin-top:10px">

<button
    class="btn gray"
    style="width:100%"
    onclick="saveConfig()"
>
💾 บันทึกข้อความ + เวลา โดยไม่เปลี่ยนสถานะ
</button>

</div>

</section>


<section class="panel">

<h2>
📡 สถานะระบบ
</h2>


<div class="system-box">

<div class="system">

<div class="system-title">
SUPABASE
</div>

<div
    id="supabaseStatus"
    class="system-value"
>
กำลังตรวจสอบ...
</div>

</div>


<div class="system">

<div class="system-title">
LOCAL BACKUP
</div>

<div
    id="backupStatus"
    class="system-value"
>
กำลังตรวจสอบ...
</div>

</div>

</div>


<div id="statusList">

<div class="status">
กำลังโหลด...
</div>

</div>


<div class="token">

<div
    style="
        font-weight:800;
        font-size:13px;
    "
>
🔑 Roblox API Token
</div>

<div
    class="sub"
    style="margin-top:4px"
>
Token จริงจะไม่ถูกฝังใน HTML หรือ JavaScript
จะส่งกลับจาก Server เฉพาะหลังยืนยัน PIN
</div>

<div
    id="tokenDisplay"
    class="token-value"
>
••••••••••••••••••••••••••••••••
</div>

<div class="token-actions">

<input
    id="pinInput"
    type="password"
    placeholder="Token PIN"
    autocomplete="off"
>

<button
    id="revealButton"
    class="btn blue"
    onclick="revealToken()"
>
👁️ ดู Token
</button>

<button
    id="newTokenButton"
    class="btn close"
    onclick="newToken()"
>
🔄 สร้างใหม่
</button>

</div>

</div>

</section>

</div>

</div>


<div
    id="notice"
    class="notice"
></div>


<script>


// ========================================================
// State
// ========================================================

let statesCache = [];

let serverOffsetMs = 0;

let busy = false;

let tokenVisible = false;


// ========================================================
// DOM
// ========================================================

const $ = id =>
    document.getElementById(id);


// ========================================================
// Notification
// ========================================================

function notify(
    message,
    ok = true
){

    const box = $(
        "notice"
    );

    box.textContent =
        message;

    box.className =
        "notice show "
        +
        (
            ok
            ? "ok"
            : "bad"
        );

    clearTimeout(
        notify.timer
    );

    notify.timer =
        setTimeout(
            () => {

                box.className =
                    "notice";

            },
            4500
        );

}


// ========================================================
// Busy
// ========================================================

function setBusy(
    value
){

    busy = value;

    document
        .querySelectorAll(
            "button"
        )
        .forEach(
            button => {

                button.disabled =
                    value;

            }
        );

}


// ========================================================
// API
// ========================================================

async function api(
    url,
    options = {}
){

    const headers =
        new Headers(
            options.headers || {}
        );

    headers.set(
        "Cache-Control",
        "no-cache"
    );

    headers.set(
        "Pragma",
        "no-cache"
    );

    if(
        options.body
        &&
        !headers.has(
            "Content-Type"
        )
    ){

        headers.set(
            "Content-Type",
            "application/json"
        );

    }

    const response =
        await fetch(
            url,
            {
                ...options,
                headers,
                cache:"no-store",
                credentials:"same-origin"
            }
        );

    let data;

    try{

        data =
            await response.json();

    }catch{

        data = {
            ok:false,
            error:
                "Server returned invalid JSON"
        };

    }

    if(
        !response.ok
        ||
        data.ok === false
    ){

        throw new Error(
            data.error
            ||
            `HTTP ${response.status}`
        );

    }

    return data;

}


// ========================================================
// Duration
// ========================================================

function fmtDuration(
    totalSeconds
){

    totalSeconds =
        Math.max(
            0,
            Math.floor(
                Number(
                    totalSeconds
                )
                || 0
            )
        );

    const d =
        Math.floor(
            totalSeconds / 86400
        );

    totalSeconds %= 86400;

    const h =
        Math.floor(
            totalSeconds / 3600
        );

    totalSeconds %= 3600;

    const m =
        Math.floor(
            totalSeconds / 60
        );

    const s =
        totalSeconds % 60;


    if(d > 0){

        return (
            `${d}d `
            +
            `${String(h).padStart(2,"0")}:`
            +
            `${String(m).padStart(2,"0")}:`
            +
            `${String(s).padStart(2,"0")}`
        );

    }


    return (
        `${String(h).padStart(2,"0")}:`
        +
        `${String(m).padStart(2,"0")}:`
        +
        `${String(s).padStart(2,"0")}`
    );

}


// ========================================================
// Clock
// ========================================================

function fmtClock(
    timestamp
){

    if(
        timestamp === null
        ||
        timestamp === undefined
        ||
        timestamp === ""
    ){

        return "-";

    }

    const number =
        Number(timestamp);

    if(
        !Number.isFinite(
            number
        )
    ){

        return "-";

    }

    return new Date(
        number * 1000
    ).toLocaleString(
        "th-TH",
        {
            year:"numeric",
            month:"2-digit",
            day:"2-digit",
            hour:"2-digit",
            minute:"2-digit",
            second:"2-digit"
        }
    );

}


// ========================================================
// Server Time
// ========================================================

function currentServerTime(){

    return (
        Date.now()
        +
        serverOffsetMs
    ) / 1000;

}


// ========================================================
// Escape HTML
// ========================================================

function escapeHtml(
    value
){

    return String(
        value ?? ""
    ).replace(
        /[&<>"']/g,
        character => ({
            "&":"&amp;",
            "<":"&lt;",
            ">":"&gt;",
            '"':"&quot;",
            "'":"&#039;"
        }[character])
    );

}


// ========================================================
// Mode
// ========================================================

function modeInfo(
    mode
){

    if(
        mode ===
        "scheduled"
    ){

        return [
            "🟠 กำลังนับถอยหลัง",
            "scheduled"
        ];

    }

    if(
        mode ===
        "closed"
    ){

        return [
            "🔴 ปิดเซิร์ฟเวอร์",
            "closed"
        ];

    }

    return [
        "🟢 เปิดให้บริการ",
        "open"
    ];

}


// ========================================================
// Status Rendering
// ========================================================

function renderStatus(
    states
){

    statesCache =
        Array.isArray(
            states
        )
        ? states
        : [];

    const list =
        $("statusList");

    if(
        !statesCache.length
    ){

        list.innerHTML =
            `
            <div class="status">
                ไม่พบข้อมูลสถานะ
            </div>
            `;

        return;

    }


    const now =
        currentServerTime();


    list.innerHTML =
        statesCache.map(
            item => {

                const [
                    label,
                    cls
                ] =
                    modeInfo(
                        item.mode
                    );


                let timer =
                    "เปิดอยู่";

                let progress =
                    0;


                if(
                    item.mode ===
                    "scheduled"
                    &&
                    item.deadline
                ){

                    const remaining =
                        Math.max(
                            0,
                            Number(
                                item.deadline
                            )
                            -
                            now
                        );

                    const total =
                        Math.max(
                            1,
                            Number(
                                item.seconds
                                || 60
                            )
                        );

                    const elapsed =
                        Math.min(
                            total,
                            Math.max(
                                0,
                                total
                                -
                                remaining
                            )
                        );

                    progress =
                        Math.min(
                            100,
                            (
                                elapsed
                                /
                                total
                            )
                            *
                            100
                        );

                    timer =
                        fmtDuration(
                            remaining
                        );

                }
                else if(
                    item.mode ===
                    "closed"
                ){

                    timer =
                        "ปิดแล้ว";

                }


                const displayName =
                    item.name
                    ||
                    item.placeId
                    ||
                    "-";


                return `
                <div
                    class="status ${cls}"
                >

                    <div class="status-head">

                        <strong>
                            ${escapeHtml(
                                displayName
                            )}
                        </strong>

                        <span
                            class="badge ${cls}"
                        >
                            ${label}
                        </span>

                    </div>

                    <div class="big">
                        สถานะปัจจุบัน
                    </div>

                    <div
                        class="timer"
                        data-timer-place="${escapeHtml(
                            item.placeId
                        )}"
                    >
                        ${timer}
                    </div>


                    ${
                        item.mode ===
                        "scheduled"
                        ?
                        `
                        <div class="progress">

                            <div
                                style="
                                    width:${progress}%
                                "
                            ></div>

                        </div>
                        `
                        :
                        ""
                    }


                    <div class="details">

                        <div class="detail">

                            <b>
                                PLACE ID
                            </b>

                            <span>
                                ${escapeHtml(
                                    item.placeId
                                )}
                            </span>

                        </div>


                        <div class="detail">

                            <b>
                                เริ่มคำสั่ง
                            </b>

                            <span>
                                ${fmtClock(
                                    item.commandStartedAt
                                    ||
                                    item.startedAt
                                )}
                            </span>

                        </div>


                        <div class="detail">

                            <b>
                                DEADLINE
                            </b>

                            <span>
                                ${fmtClock(
                                    item.deadline
                                )}
                            </span>

                        </div>


                        <div class="detail">

                            <b>
                                ระยะเวลา
                            </b>

                            <span>
                                ${fmtDuration(
                                    item.seconds
                                )}
                            </span>

                        </div>


                        <div class="detail">

                            <b>
                                เหลือเวลา
                            </b>

                            <span>
                                ${
                                    item.mode ===
                                    "scheduled"
                                    ?
                                    fmtDuration(
                                        Math.max(
                                            0,
                                            Number(
                                                item.deadline
                                            )
                                            -
                                            now
                                        )
                                    )
                                    :
                                    "00:00:00"
                                }
                            </span>

                        </div>


                        <div class="detail">

                            <b>
                                ผ่านไปแล้ว
                            </b>

                            <span>
                                ${fmtDuration(
                                    item.elapsed
                                    || 0
                                )}
                            </span>

                        </div>

                    </div>


                    <div class="reason">

                        📝
                        ${escapeHtml(
                            item.reason
                        )}

                    </div>

                </div>
                `;

            }
        ).join("");

}


// ========================================================
// Local Countdown
// ========================================================

function updateTimers(){

    const now =
        currentServerTime();


    statesCache.forEach(
        item => {

            if(
                item.mode !==
                "scheduled"
                ||
                !item.deadline
            ){

                return;

            }


            const selector =
                `[data-timer-place="${CSS.escape(
                    String(
                        item.placeId
                    )
                )}"]`;


            const element =
                document.querySelector(
                    selector
                );


            if(!element){
                return;
            }


            const remaining =
                Math.max(
                    0,
                    Number(
                        item.deadline
                    )
                    -
                    now
                );


            element.textContent =
                fmtDuration(
                    remaining
                );

        }
    );

}


// ========================================================
// System Status
// ========================================================

function updateSystemStatus(
    data
){

    const db =
        $("supabaseStatus");

    const backup =
        $("backupStatus");

    const badge =
        $("dbBadge");


    if(
        data.dbAvailable
    ){

        db.textContent =
            "● เชื่อมต่อปกติ";

        db.className =
            "system-value ok";

        badge.textContent =
            "● DATABASE OK";

        badge.className =
            "badge";

    }
    else{

        db.textContent =
            "● Supabase Offline";

        db.className =
            "system-value off";

        badge.textContent =
            "● DATABASE OFFLINE";

        badge.className =
            "badge closed";

    }


    if(
        data.backupAvailable
    ){

        backup.textContent =
            "● Backup พร้อมใช้";

        backup.className =
            "system-value ok";

    }
    else{

        backup.textContent =
            "● Backup ไม่พร้อม";

        backup.className =
            "system-value off";

    }

}


// ========================================================
// Load Dashboard Status
// ========================================================

let statusLoading = false;


async function loadStatus(){

    if(statusLoading){
        return;
    }

    statusLoading = true;

    try{

        const data =
            await api(
                "/api/status?_="
                +
                Date.now()
            );


        const serverNow =
            Number(
                data.serverNow
            ) * 1000;


        if(
            Number.isFinite(
                serverNow
            )
        ){

            serverOffsetMs =
                serverNow
                -
                Date.now();

        }


        updateSystemStatus(
            data
        );

        renderStatus(
            data.states
        );

    }
    catch(error){

        console.error(
            error
        );

    }
    finally{

        statusLoading = false;

    }

}


// ========================================================
// Load Selected Map
// ========================================================

async function loadSelected(){

    const selected =
        $("mapSelect").value;


    if(
        selected ===
        "ALL"
    ){

        $("reasonInput").value =
            "";

        $("secondsInput").value =
            "60";

        return;

    }


    try{

        const data =
            await api(
                "/api/state/"
                +
                encodeURIComponent(
                    selected
                )
                +
                "?_="
                +
                Date.now()
            );


        $("reasonInput").value =
            data.state.reason
            ||
            "";

        $("secondsInput").value =
            data.state.seconds
            ||
            60;

    }
    catch(error){

        notify(
            "โหลดข้อมูลแมพไม่สำเร็จ: "
            +
            error.message,
            false
        );

    }

}


// ========================================================
// Read Form
// ========================================================

function readForm(){

    const reason =
        $("reasonInput")
            .value
            .trim();


    const rawSeconds =
        $("secondsInput")
            .value
            .trim();


    if(
        !/^\d+$/.test(
            rawSeconds
        )
    ){

        throw new Error(
            "เวลาต้องเป็นตัวเลขจำนวนเต็ม"
        );

    }


    const seconds =
        Number(
            rawSeconds
        );


    if(
        !Number.isInteger(
            seconds
        )
        ||
        seconds < 1
        ||
        seconds > 86400
    ){

        throw new Error(
            "เวลาต้องอยู่ระหว่าง 1 - 86400 วินาที"
        );

    }


    return {
        reason,
        seconds
    };

}


// ========================================================
// Selected Action
// ========================================================

async function actionSelected(
    mode
){

    if(busy){
        return;
    }


    const selected =
        $("mapSelect").value;


    if(
        selected ===
        "ALL"
    ){

        notify(
            "กรุณาเลือกแมพก่อน",
            false
        );

        return;

    }


    try{

        const form =
            readForm();


        if(
            mode ===
            "scheduled"
            &&
            !$("timerEnabled").checked
        ){

            mode =
                "closed";

        }


        setBusy(true);


        await api(
            "/api/update",
            {
                method:"POST",
                body:JSON.stringify({
                    place_id:selected,
                    mode:mode,
                    reason:form.reason,
                    seconds:form.seconds
                })
            }
        );


        notify(
            "บันทึกคำสั่งสำเร็จ ✅"
        );


        await loadStatus();

    }
    catch(error){

        notify(
            error.message,
            false
        );

    }
    finally{

        setBusy(false);

    }

}


// ========================================================
// All Action
// ========================================================

async function actionAll(
    mode
){

    if(busy){
        return;
    }


    try{

        const form =
            readForm();


        if(
            mode ===
            "scheduled"
            &&
            !$("timerEnabled").checked
        ){

            mode =
                "closed";

        }


        setBusy(true);


        const data =
            await api(
                "/api/update-all",
                {
                    method:"POST",
                    body:JSON.stringify({
                        mode:mode,
                        reason:form.reason,
                        seconds:form.seconds
                    })
                }
            );


        if(
            data.failed
            &&
            data.failed > 0
        ){

            notify(
                `ดำเนินการบางแมพไม่สำเร็จ (${data.failed})`,
                false
            );

        }
        else{

            notify(
                "บันทึกคำสั่งทุกแมพสำเร็จ ✅"
            );

        }


        await loadStatus();

    }
    catch(error){

        notify(
            error.message,
            false
        );

    }
    finally{

        setBusy(false);

    }

}


// ========================================================
// Save Config
// ========================================================

async function saveConfig(){

    if(busy){
        return;
    }


    try{

        const selected =
            $("mapSelect").value;

        const form =
            readForm();


        setBusy(true);


        if(
            selected ===
            "ALL"
        ){

            await api(
                "/api/save-config",
                {
                    method:"POST",
                    body:JSON.stringify({
                        scope:"all",
                        reason:form.reason,
                        seconds:form.seconds
                    })
                }
            );

        }
        else{

            await api(
                "/api/save-config",
                {
                    method:"POST",
                    body:JSON.stringify({
                        scope:"place",
                        place_id:selected,
                        reason:form.reason,
                        seconds:form.seconds
                    })
                }
            );

        }


        notify(
            "บันทึกข้อความและเวลาแล้ว ✅"
        );


        await loadStatus();

    }
    catch(error){

        notify(
            error.message,
            false
        );

    }
    finally{

        setBusy(false);

    }

}


// ========================================================
// Reveal Token
// ========================================================

async function revealToken(){

    if(busy){
        return;
    }


    if(tokenVisible){

        $("tokenDisplay")
            .textContent =
            "••••••••••••••••••••••••••••••••";

        tokenVisible =
            false;

        $("revealButton")
            .textContent =
            "👁️ ดู Token";

        return;

    }


    const pin =
        $("pinInput").value;


    if(!pin){

        notify(
            "กรุณากรอก Token PIN",
            false
        );

        return;

    }


    try{

        setBusy(true);


        const data =
            await api(
                "/api/token/reveal",
                {
                    method:"POST",
                    body:JSON.stringify({
                        pin:pin
                    })
                }
            );


        // Token is received only now.
        // It is NOT inside initial HTML.

        $("tokenDisplay")
            .textContent =
            data.token;


        tokenVisible =
            true;


        $("revealButton")
            .textContent =
            "🙈 ซ่อน Token";


        $("pinInput").value =
            "";


        notify(
            "ยืนยัน PIN สำเร็จ"
        );

    }
    catch(error){

        $("tokenDisplay")
            .textContent =
            "••••••••••••••••••••••••••••••••";

        tokenVisible =
            false;

        notify(
            error.message,
            false
        );

    }
    finally{

        setBusy(false);

    }

}


// ========================================================
// Regenerate Token
// ========================================================

async function newToken(){

    if(busy){
        return;
    }


    const pin =
        $("pinInput").value;


    if(!pin){

        notify(
            "กรุณากรอก Token PIN ก่อนสร้าง Token ใหม่",
            false
        );

        return;

    }


    if(
        !confirm(
            "สร้าง Token ใหม่จริงหรือไม่?\n\n"
            +
            "Token เดิมจะใช้งานไม่ได้อีก"
        )
    ){

        return;

    }


    try{

        setBusy(true);


        const data =
            await api(
                "/api/token/regenerate",
                {
                    method:"POST",
                    body:JSON.stringify({
                        pin:pin
                    })
                }
            );


        $("tokenDisplay")
            .textContent =
            data.token;


        $("pinInput").value =
            "";


        tokenVisible =
            true;


        $("revealButton")
            .textContent =
            "🙈 ซ่อน Token";


        notify(
            "สร้าง Token ใหม่สำเร็จและบันทึกแล้ว ✅"
        );

    }
    catch(error){

        notify(
            error.message,
            false
        );

    }
    finally{

        setBusy(false);

    }

}


// ========================================================
// Auto Refresh
// ========================================================

setInterval(
    loadStatus,
    2000
);


// ========================================================
// Local Timer Refresh
// ========================================================

setInterval(
    updateTimers,
    250
);


// ========================================================
// Initial
// ========================================================

loadStatus();

</script>

</body>

</html>
"""


# ============================================================
# Login
# ============================================================

@app.route(
    "/",
    methods=["GET", "POST"],
)
def login():

    if logged_in():

        return redirect(
            "/dashboard"
        )

    error = None

    if request.method == "POST":

        password = request.form.get(
            "password",
            "",
        )

        if safe_equal(
            password,
            ADMIN_PASSWORD,
        ):

            session.clear()

            session.permanent = True

            session[
                "admin_authenticated"
            ] = True

            session[
                "login_time"
            ] = time.time()

            return redirect(
                "/dashboard"
            )

        error = (
            "รหัสผ่านไม่ถูกต้อง"
        )

    return render_template_string(
        LOGIN_HTML,
        error=error,
    )


# ============================================================
# Dashboard
# ============================================================

@app.route(
    "/dashboard",
    methods=["GET"],
)
@login_required
def dashboard():

    ensure_db_loaded()

    return render_template_string(
        DASHBOARD_HTML,
        maps=MAPS,
        default_reason=DEFAULT_REASON,
    )


# ============================================================
# Logout
# ============================================================

@app.route(
    "/logout",
    methods=["GET"],
)
def logout():

    session.clear()

    return redirect("/")


# ============================================================
# API Status
# ============================================================

@app.route(
    "/api/status",
    methods=["GET"],
)
@login_required
def api_status():

    ensure_db_loaded()

    states = []

    for name, pid in MAPS.items():

        state = snapshot(
            pid
        )

        state["name"] = name

        state["placeId"] = pid

        states.append(
            state
        )

    return jsonify({
        "ok": True,
        "serverNow": time.time(),
        "dbAvailable": DB_AVAILABLE,
        "backupAvailable": BACKUP_AVAILABLE,
        "lastDbSync": LAST_DB_SYNC,
        "lastDbError": LAST_DB_ERROR,
        "lastBackupSave": LAST_BACKUP_SAVE,
        "lastBackupError": LAST_BACKUP_ERROR,
        "initialized": INITIALIZED,
        "states": states,
    })


# ============================================================
# API Individual State
# ============================================================

@app.route(
    "/api/state/<place_id>",
    methods=["GET"],
)
@login_required
def api_state(
    place_id
):

    if place_id not in PLACE_IDS:

        return jsonify({
            "ok": False,
            "error": "ไม่พบ Place ID นี้",
        }), 404

    return jsonify({
        "ok": True,
        "serverNow": time.time(),
        "state": snapshot(
            place_id
        ),
    })


# ============================================================
# Public Roblox State API
# ============================================================

@app.route(
    "/state/<place_id>",
    methods=["GET"],
)
def roblox_state(
    place_id
):

    if place_id not in PLACE_IDS:

        return jsonify({
            "ok": False,
            "error": "unknown place",
        }), 404

    state = snapshot(
        place_id
    )

    return jsonify({
        "mode": state["mode"],
        "reason": state["reason"],
        "deadline": state["deadline"],
        "seconds": state["seconds"],
        "serverNow": state["serverNow"],
        "remaining": state["remaining"],
        "elapsed": state["elapsed"],
    })


# ============================================================
# API Update
# ============================================================

@app.route(
    "/api/update",
    methods=["POST"],
)
@login_required
def api_update():

    try:

        data = request_json()

        place_id = str(
            data.get(
                "place_id",
                "",
            )
        ).strip()

        mode = data.get(
            "mode"
        )

        reason = data.get(
            "reason"
        )

        seconds = data.get(
            "seconds"
        )


        if place_id not in PLACE_IDS:

            raise ValueError(
                "ไม่พบ Place ID นี้"
            )


        if mode not in {
            "open",
            "scheduled",
            "closed",
        }:

            raise ValueError(
                "สถานะไม่ถูกต้อง"
            )


        result = update_state(
            place_id,
            mode=mode,
            reason=reason,
            seconds=seconds,
        )


        return jsonify({
            "ok": True,
            "state": result,
        })


    except Exception as exc:

        LOG.error(
            "api_update failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 400


# ============================================================
# API Update All
# ============================================================

@app.route(
    "/api/update-all",
    methods=["POST"],
)
@login_required
def api_update_all():

    try:

        data = request_json()

        mode = data.get(
            "mode"
        )

        reason = data.get(
            "reason"
        )

        seconds = data.get(
            "seconds"
        )


        result = update_all(
            mode,
            reason=reason,
            seconds=seconds,
        )


        return jsonify({
            "ok": result["success"],
            "results": result["results"],
            "failed": result["failed"],
        })


    except Exception as exc:

        LOG.error(
            "api_update_all failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 400


# ============================================================
# API Save Config
# ============================================================

@app.route(
    "/api/save-config",
    methods=["POST"],
)
@login_required
def api_save_config():

    try:

        data = request_json()

        scope = data.get(
            "scope",
            "place",
        )

        reason = normalize_reason(
            data.get("reason")
        )

        seconds = normalize_seconds(
            data.get(
                "seconds",
                60,
            )
        )


        # ====================================================
        # ALL
        # ====================================================

        if scope == "all":

            results = []

            with WRITE_LOCK:

                for pid in PLACE_IDS:

                    with LOCK:

                        current = copy.deepcopy(
                            DATA["places"][pid]
                        )

                    # ----------------------------------------
                    # IMPORTANT:
                    #
                    # เปลี่ยนเฉพาะ:
                    #   reason
                    #   seconds
                    #
                    # ไม่เปลี่ยน:
                    #   mode
                    #   deadline
                    #   commandStartedAt
                    # ----------------------------------------

                    new_state = copy.deepcopy(
                        current
                    )

                    new_state["reason"] = (
                        normalize_reason(
                            reason
                        )
                    )

                    new_state["seconds"] = (
                        normalize_seconds(
                            seconds
                        )
                    )

                    new_state = clean_state(
                        new_state
                    )

                    # ----------------------------------------
                    # DB FIRST
                    # ----------------------------------------

                    ok, error = save_with_retry(
                        pid,
                        new_state,
                    )

                    if not ok:

                        raise RuntimeError(
                            f"{pid}: "
                            f"{error}"
                        )

                    # ----------------------------------------
                    # DB confirmed.
                    # Only now update RAM.
                    # ----------------------------------------

                    with LOCK:

                        DATA["places"][pid] = (
                            copy.deepcopy(
                                new_state
                            )
                        )

                    results.append(
                        snapshot(pid)
                    )

            # ----------------------------------------------
            # Backup AFTER all DB writes succeeded
            # ----------------------------------------------

            backup_ok, backup_error = (
                save_local_backup()
            )

            if not backup_ok:

                LOG.warning(
                    "Config DB saved but backup failed: %s",
                    backup_error,
                )

            return jsonify({
                "ok": True,
                "results": results,
            })


        # ====================================================
        # SINGLE
        # ====================================================

        place_id = str(
            data.get(
                "place_id",
                "",
            )
        ).strip()


        if place_id not in PLACE_IDS:

            raise ValueError(
                "ไม่พบ Place ID นี้"
            )


        with WRITE_LOCK:

            with LOCK:

                current = copy.deepcopy(
                    DATA["places"][place_id]
                )

            # ----------------------------------------------
            # IMPORTANT:
            #
            # แก้เฉพาะ reason + seconds
            #
            # ค่าเหล่านี้ยังคงเดิม:
            # mode
            # deadline
            # commandStartedAt
            # ----------------------------------------------

            new_state = copy.deepcopy(
                current
            )

            new_state["reason"] = (
                normalize_reason(
                    reason
                )
            )

            new_state["seconds"] = (
                normalize_seconds(
                    seconds
                )
            )

            new_state = clean_state(
                new_state
            )


            # ------------------------------------------------
            # DB FIRST
            # ------------------------------------------------

            ok, error = save_with_retry(
                place_id,
                new_state,
            )


            if not ok:

                raise RuntimeError(
                    "บันทึกไม่สำเร็จ: "
                    + str(error)
                )


            # ------------------------------------------------
            # DB confirmed -> RAM
            # ------------------------------------------------

            with LOCK:

                DATA["places"][place_id] = (
                    copy.deepcopy(
                        new_state
                    )
                )


        # ----------------------------------------------------
        # Backup after DB success
        # ----------------------------------------------------

        backup_ok, backup_error = (
            save_local_backup()
        )

        if not backup_ok:

            LOG.warning(
                "Config DB saved but backup failed: %s",
                backup_error,
            )


        # ----------------------------------------------------
        # Return freshly generated snapshot
        # ----------------------------------------------------

        return jsonify({
            "ok": True,
            "state": snapshot(
                place_id
            ),
        })


    except Exception as exc:

        LOG.error(
            "api_save_config failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 400


# ============================================================
# API Reveal Token
# ============================================================

@app.route(
    "/api/token/reveal",
    methods=["POST"],
)
@login_required
def api_token_reveal():

    try:

        data = request_json()

        pin = str(
            data.get(
                "pin",
                "",
            )
        )


        if not verify_token_pin(
            pin
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Token PIN ไม่ถูกต้อง",
            }), 403


        ensure_db_loaded()


        with LOCK:

            token = DATA.get(
                "token"
            )


        # ----------------------------------------------------
        # Try Supabase
        # ----------------------------------------------------

        if not token:

            try:

                loaded = (
                    load_from_supabase()
                )

                token = loaded.get(
                    "token"
                )

                if token:

                    with LOCK:

                        DATA["token"] = (
                            token
                        )

            except Exception as exc:

                LOG.warning(
                    "Token Supabase load failed: %s",
                    exc,
                )


        # ----------------------------------------------------
        # Try Backup
        # ----------------------------------------------------

        if not token:

            backup = (
                load_local_backup()
            )

            if backup:

                token = backup.get(
                    "token"
                )


        if not token:

            return jsonify({
                "ok": False,
                "error":
                    "ไม่พบ Token ในระบบ",
            }), 500


        # IMPORTANT:
        #
        # Token is NOT in:
        # - HTML
        # - initial JavaScript
        # - Flask session
        #
        # It is returned ONLY after PIN verification.
        #

        return jsonify({
            "ok": True,
            "token": token,
        })


    except Exception as exc:

        LOG.error(
            "api_token_reveal failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 400


# ============================================================
# API Regenerate Token
# ============================================================

@app.route(
    "/api/token/regenerate",
    methods=["POST"],
)
@login_required
def api_token_regenerate():

    try:

        data = request_json()

        pin = str(
            data.get(
                "pin",
                "",
            )
        )


        if not verify_token_pin(
            pin
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Token PIN ไม่ถูกต้อง",
            }), 403


        new_token = secrets.token_urlsafe(
            32
        )


        # ----------------------------------------------------
        # Save new token FIRST
        # ----------------------------------------------------

        ok, error = save_with_retry(
            SYSTEM_TOKEN_ID,
            {
                "token": new_token,
            },
        )


        if not ok:

            raise RuntimeError(
                "บันทึก Token ใหม่ใน Supabase "
                "ไม่สำเร็จ: "
                + str(error)
            )


        # ----------------------------------------------------
        # Supabase success -> RAM
        # ----------------------------------------------------

        with LOCK:

            DATA["token"] = new_token


        # ----------------------------------------------------
        # Backup
        # ----------------------------------------------------

        backup_ok, backup_error = (
            save_local_backup()
        )


        if not backup_ok:

            LOG.warning(
                "Token DB saved but backup failed: %s",
                backup_error,
            )


        return jsonify({
            "ok": True,
            "token": new_token,
        })


    except Exception as exc:

        LOG.error(
            "api_token_regenerate failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 400


# ============================================================
# Health
# ============================================================

@app.route(
    "/health",
    methods=["GET"],
)
def health():

    return jsonify({
        "ok": True,
        "service":
            "roblox-control-center",
        "database":
            DB_AVAILABLE,
        "backup":
            BACKUP_AVAILABLE,
        "initialized":
            INITIALIZED,
        "serverNow":
            time.time(),
    })


# ============================================================
# Startup
# ============================================================

def startup():

    LOG.info(
        "Starting Roblox Control Center..."
    )

    LOG.info(
        "Supabase: %s",
        SUPABASE_URL,
    )

    LOG.info(
        "Backup file: %s",
        os.path.abspath(
            BACKUP_FILE
        ),
    )

    initialize_database()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    startup()

    LOG.info(
        "Server listening on %s:%s",
        HOST,
        PORT,
    )

    serve(
        app,
        host=HOST,
        port=PORT,
        threads=8,
    )
