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
# Supabase table: control_state
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

SYSTEM_TOKEN_ID = "**SYS_TOKEN**"


# ============================================================
# Limits
# ============================================================

MIN_SECONDS = 1
MAX_SECONDS = 86400

MAX_REASON_LENGTH = 500

SUPABASE_TIMEOUT = 8

SAVE_RETRIES = 3

VERIFY_RETRIES = 3

VERIFY_DELAY = 0.20

BACKUP_VERSION = 2


# ============================================================
# Locks
# ============================================================

LOCK = threading.RLock()

# สำคัญ:
# ทุกการเขียน Supabase ต้องผ่าน lock นี้
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
        ) == "1"
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
# No Cache
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
        # Ignore unknown Place
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
# Build Supabase Payload
# ============================================================

def build_supabase_payload(
    place_id,
    state_data,
):

    if place_id == SYSTEM_TOKEN_ID:

        return {
            "place_id": SYSTEM_TOKEN_ID,
            "mode": "token",
            "reason": str(
                state_data["token"]
            ),
            "deadline": None,
            "seconds": 0,
            "command_started_at": None,
        }

    state_data = clean_state(
        state_data
    )

    return {
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


# ============================================================
# Save To Supabase
#
# IMPORTANT:
# Save -> Return Representation -> Verify
#
# นี่คือส่วนสำคัญที่แก้ปัญหา Save แล้วกลับค่าเก่า
# ============================================================

def save_to_supabase(
    place_id,
    state_data,
):

    payload = build_supabase_payload(
        place_id,
        state_data,
    )

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
    # Token
    # --------------------------------------------------------

    if place_id == SYSTEM_TOKEN_ID:

        if not isinstance(result, list):
            raise RuntimeError(
                "Supabase ไม่คืนข้อมูล Token หลังบันทึก"
            )

        if not result:
            raise RuntimeError(
                "Supabase ไม่พบข้อมูล Token หลังบันทึก"
            )

        saved_token = str(
            result[0].get(
                "reason",
                ""
            )
        )

        if saved_token != str(
            state_data["token"]
        ):
            raise RuntimeError(
                "Token ที่ Supabase บันทึก "
                "ไม่ตรงกับ Token ใหม่"
            )

        return result

    # --------------------------------------------------------
    # Normal State
    # --------------------------------------------------------

    if not isinstance(result, list):

        raise RuntimeError(
            "Supabase ไม่คืนข้อมูลหลังบันทึก"
        )

    if not result:

        raise RuntimeError(
            "Supabase ไม่คืนแถวที่บันทึกกลับมา"
        )

    saved = result[0]

    # --------------------------------------------------------
    # Verify place_id
    # --------------------------------------------------------

    if str(
        saved.get("place_id")
    ) != place_id:

        raise RuntimeError(
            "Supabase บันทึกผิด place_id"
        )

    # --------------------------------------------------------
    # Verify mode
    # --------------------------------------------------------

    if saved.get(
        "mode"
    ) != payload["mode"]:

        raise RuntimeError(
            "Supabase บันทึก mode "
            "ไม่ตรงกับค่าที่ส่ง"
        )

    # --------------------------------------------------------
    # Verify reason
    # --------------------------------------------------------

    saved_reason = normalize_reason(
        saved.get("reason")
    )

    expected_reason = normalize_reason(
        payload["reason"]
    )

    if saved_reason != expected_reason:

        raise RuntimeError(
            "Supabase บันทึก reason "
            "ไม่ตรงกับค่าที่ส่ง"
        )

    # --------------------------------------------------------
    # Verify seconds
    # --------------------------------------------------------

    try:

        saved_seconds = normalize_seconds(
            saved.get(
                "seconds",
                60,
            )
        )

    except ValueError:

        raise RuntimeError(
            "Supabase ส่งค่า seconds "
            "ที่ไม่ถูกต้องกลับมา"
        )

    if saved_seconds != payload["seconds"]:

        raise RuntimeError(
            "Supabase บันทึก seconds "
            "ไม่ตรงกับค่าที่ส่ง"
        )

    # --------------------------------------------------------
    # Verify deadline
    # --------------------------------------------------------

    saved_deadline = normalize_timestamp(
        saved.get("deadline")
    )

    expected_deadline = normalize_timestamp(
        payload["deadline"]
    )

    if (
        saved_deadline is None
        and expected_deadline is None
    ):
        pass

    elif (
        saved_deadline is None
        or expected_deadline is None
    ):

        raise RuntimeError(
            "Supabase บันทึก deadline "
            "ไม่ตรงกับค่าที่ส่ง"
        )

    elif abs(
        saved_deadline
        - expected_deadline
    ) > 0.001:

        raise RuntimeError(
            "Supabase บันทึก deadline "
            "ไม่ตรงกับค่าที่ส่ง"
        )

    return saved


# ============================================================
# Save With Retry
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

            saved = save_to_supabase(
                place_id,
                state_data,
            )

            DB_AVAILABLE = True
            LAST_DB_SYNC = time.time()
            LAST_DB_ERROR = None

            return True, None, saved

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

    return False, last_error, None


# ============================================================
# Verify Database State
#
# หลังเขียนเสร็จ อ่านกลับอีกครั้ง
# เพื่อป้องกันกรณีมี process/server อื่นเขียนทับ
# ============================================================

def verify_database_state(
    place_id,
    expected_state,
    retries=VERIFY_RETRIES,
):

    if place_id == SYSTEM_TOKEN_ID:

        expected_token = str(
            expected_state["token"]
        )

        for attempt in range(
            1,
            retries + 1,
        ):

            try:

                rows = supabase_request(
                    "GET",
                    "control_state",
                    query={
                        "select": "place_id,reason",
                        "place_id": f"eq.{SYSTEM_TOKEN_ID}",
                    },
                )

                if (
                    isinstance(rows, list)
                    and rows
                ):

                    actual = str(
                        rows[0].get(
                            "reason",
                            ""
                        )
                    )

                    if actual == expected_token:
                        return True, None

            except Exception as exc:

                LOG.warning(
                    "Token verification failed: %s",
                    exc,
                )

            if attempt < retries:
                time.sleep(
                    VERIFY_DELAY
                )

        return False, (
            "ตรวจสอบ Token หลังบันทึกไม่สำเร็จ"
        )

    expected = clean_state(
        expected_state
    )

    for attempt in range(
        1,
        retries + 1,
    ):

        try:

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
                    "place_id": (
                        f"eq.{place_id}"
                    ),
                },
            )

            if (
                not isinstance(rows, list)
                or not rows
            ):

                raise RuntimeError(
                    "ไม่พบข้อมูลใน Supabase"
                )

            row = rows[0]

            actual = clean_state({
                "mode": row.get(
                    "mode"
                ),

                "reason": row.get(
                    "reason"
                ),

                "deadline": row.get(
                    "deadline"
                ),

                "seconds": row.get(
                    "seconds"
                ),

                "commandStartedAt": row.get(
                    "command_started_at"
                ),
            })

            # ------------------------------------------------
            # Compare
            # ------------------------------------------------

            if actual["mode"] != expected["mode"]:

                raise RuntimeError(
                    "mode ไม่ตรง"
                )

            if actual["reason"] != expected["reason"]:

                raise RuntimeError(
                    "reason ไม่ตรง"
                )

            if actual["seconds"] != expected["seconds"]:

                raise RuntimeError(
                    "seconds ไม่ตรง"
                )

            # Deadline
            expected_deadline = expected[
                "deadline"
            ]

            actual_deadline = actual[
                "deadline"
            ]

            if (
                expected_deadline is None
                and actual_deadline is None
            ):
                pass

            elif (
                expected_deadline is None
                or actual_deadline is None
            ):

                raise RuntimeError(
                    "deadline ไม่ตรง"
                )

            elif abs(
                expected_deadline
                - actual_deadline
            ) > 0.001:

                raise RuntimeError(
                    "deadline ไม่ตรง"
                )

            return True, None

        except Exception as exc:

            last_error = str(exc)

            LOG.warning(
                "Database verification "
                "(%s/%s) failed for %s: %s",
                attempt,
                retries,
                place_id,
                last_error,
            )

            if attempt < retries:

                time.sleep(
                    VERIFY_DELAY
                )

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
    # Create Token
    # --------------------------------------------------------

    if not DATA.get("token"):

        new_token = secrets.token_urlsafe(
            32
        )

        ok, error, _ = save_with_retry(
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
    # Create Missing Places
    # --------------------------------------------------------

    for pid in loaded.get(
        "missing",
        [],
    ):

        state = default_state()

        ok, error, _ = save_with_retry(
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
    # Backup
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

def build_expired_state(state):

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

            # ถ้ามีคนแก้ state แล้ว ไม่ทำอะไร
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

            new_state["deadline"] = (
                expected_deadline
            )

            ok, error, _ = save_with_retry(
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

            # ------------------------------------------------
            # Verify อีกครั้ง
            # ------------------------------------------------

            verified, verify_error = (
                verify_database_state(
                    place_id,
                    new_state,
                )
            )

            if not verified:

                LOG.error(
                    "Transition verification failed "
                    "for %s: %s",
                    place_id,
                    verify_error,
                )

                return

            # ------------------------------------------------
            # DB OK -> RAM
            # ------------------------------------------------

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

    # --------------------------------------------------------
    # Reason
    # --------------------------------------------------------

    if reason is not None:

        new_state["reason"] = (
            normalize_reason(
                reason
            )
        )

    # --------------------------------------------------------
    # Seconds
    # --------------------------------------------------------

    if seconds is not None:

        new_state["seconds"] = (
            normalize_seconds(
                seconds
            )
        )

    # --------------------------------------------------------
    # Mode
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # Scheduled
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Closed
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Open
        # ----------------------------------------------------

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
        # DB FIRST
        # ----------------------------------------------------

        ok, error, _ = save_with_retry(
            place_id,
            new_state,
        )

        if not ok:

            raise RuntimeError(
                "บันทึก Supabase ไม่สำเร็จ: "
                + str(error)
            )

        # ----------------------------------------------------
        # Read-after-write verification
        # ----------------------------------------------------

        verified, verify_error = (
            verify_database_state(
                place_id,
                new_state,
            )
        )

        if not verified:

            raise RuntimeError(
                "บันทึกแล้ว แต่ตรวจสอบข้อมูลใน "
                "Supabase ไม่ผ่าน: "
                + str(verify_error)
            )

        # ----------------------------------------------------
        # DB + verification OK
        # ----------------------------------------------------

        with LOCK:

            DATA["places"][place_id] = (
                copy.deepcopy(
                    new_state
                )
            )

        # ----------------------------------------------------
        # Backup
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
# LOGIN HTML
# ============================================================

LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="th">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Roblox Control Center</title>

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

    background:
        radial-gradient(
            circle at top,
            #252525,
            #090909 65%
        );

    color: white;
    font-family:
        Arial,
        "Noto Sans Thai",
        sans-serif;
}

.login-card {

    width: min(420px, calc(100% - 30px));

    padding: 35px;

    background: rgba(20,20,20,.95);

    border: 1px solid #333;

    border-radius: 22px;

    box-shadow:
        0 25px 80px rgba(0,0,0,.55);
}

.logo {

    font-size: 28px;
    font-weight: 800;

    margin-bottom: 8px;
}

.subtitle {

    color: #999;

    margin-bottom: 28px;
}

input {

    width: 100%;

    padding: 15px;

    border-radius: 12px;

    border: 1px solid #444;

    background: #111;

    color: white;

    outline: none;

    font-size: 15px;
}

input:focus {

    border-color: #777;

}

button {

    width: 100%;

    margin-top: 14px;

    padding: 14px;

    border: 0;

    border-radius: 12px;

    cursor: pointer;

    font-weight: 700;

    background: white;

    color: black;
}

.error {

    margin-top: 15px;

    padding: 12px;

    border-radius: 10px;

    background: rgba(255,50,50,.12);

    color: #ff7373;
}

</style>

</head>

<body>

<div class="login-card">

    <div class="logo">
        🎮 Roblox Control Center
    </div>

    <div class="subtitle">
        ระบบควบคุมสถานะเซิร์ฟเวอร์
    </div>

    <form method="POST">

        <input
            type="password"
            name="password"
            autocomplete="current-password"
            placeholder="กรอกรหัสผ่าน"
            required
            autofocus
        >

        <button type="submit">
            🔐 เข้าสู่ระบบ
        </button>

    </form>

    {% if error %}

        <div class="error">
            {{ error }}
        </div>

    {% endif %}

</div>

</body>
</html>
"""


# ============================================================
# DASHBOARD HTML
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>

<html lang="th">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Roblox Control Center</title>

<style>

* {
    box-sizing: border-box;
}

:root {
    --bg: #080808;
    --card: #111;
    --card2: #171717;
    --border: #292929;
    --text: #f5f5f5;
    --muted: #8d8d8d;
    --green: #39d98a;
    --red: #ff5d5d;
    --blue: #5da9ff;
    --yellow: #ffc857;
}

body {

    margin: 0;

    min-height: 100vh;

    background:
        radial-gradient(
            circle at 20% 0%,
            #222,
            transparent 35%
        ),
        radial-gradient(
            circle at 90% 10%,
            #171717,
            transparent 30%
        ),
        var(--bg);

    color: var(--text);

    font-family:
        Arial,
        "Noto Sans Thai",
        sans-serif;
}

.container {

    width: min(1100px, calc(100% - 28px));

    margin: auto;

    padding: 25px 0 50px;
}

.header {

    display: flex;

    justify-content: space-between;

    align-items: center;

    gap: 15px;

    margin-bottom: 22px;
}

.title {

    font-size: clamp(24px, 4vw, 38px);

    font-weight: 900;
}

.subtitle {

    color: var(--muted);

    margin-top: 5px;
}

.logout {

    color: #aaa;

    text-decoration: none;

    padding: 10px 14px;

    border: 1px solid var(--border);

    border-radius: 10px;
}

.statusbar {

    display: grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap: 12px;

    margin-bottom: 15px;
}

.status {

    background: var(--card);

    border: 1px solid var(--border);

    border-radius: 14px;

    padding: 14px;
}

.status-label {

    color: var(--muted);

    font-size: 12px;

    margin-bottom: 6px;
}

.status-value {

    font-weight: 800;
}

.card {

    background:
        linear-gradient(
            180deg,
            rgba(255,255,255,.035),
            rgba(255,255,255,.015)
        ),
        var(--card);

    border: 1px solid var(--border);

    border-radius: 18px;

    padding: 20px;

    margin-bottom: 15px;

    box-shadow:
        0 15px 50px rgba(0,0,0,.2);
}

.section-title {

    font-size: 18px;

    font-weight: 800;

    margin-bottom: 15px;
}

label {

    display: block;

    color: #bbb;

    font-size: 13px;

    margin-bottom: 7px;
}

select,
input[type="text"],
input[type="number"],
input[type="password"] {

    width: 100%;

    padding: 13px 14px;

    background: #0c0c0c;

    border: 1px solid #333;

    color: white;

    border-radius: 11px;

    outline: none;

    font-size: 15px;
}

select:focus,
input:focus {

    border-color: #777;
}

.field {

    margin-bottom: 15px;
}

.grid {

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 14px;
}

button {

    border: 0;

    cursor: pointer;

    border-radius: 11px;

    padding: 13px 15px;

    font-weight: 800;

    transition:
        transform .1s,
        opacity .1s;
}

button:active {

    transform: scale(.98);
}

button:disabled {

    opacity: .5;

    cursor: not-allowed;
}

.btn-row {

    display: grid;

    grid-template-columns:
        repeat(2, 1fr);

    gap: 10px;

    margin-top: 10px;
}

.btn {

    color: white;

    background: #272727;
}

.btn.open {

    background: #155b39;
}

.btn.close {

    background: #722828;
}

.btn.cancel {

    background: #333;
}

.btn.blue {

    background: #194d7d;
}

.btn.gray {

    background: #242424;
}

.all {

    width: 100%;
}

.state-box {

    background: #0b0b0b;

    border: 1px solid #292929;

    border-radius: 14px;

    padding: 17px;

    margin-top: 16px;
}

.state-main {

    display: flex;

    justify-content: space-between;

    align-items: center;

    gap: 15px;
}

.badge {

    display: inline-flex;

    align-items: center;

    gap: 7px;

    padding: 7px 11px;

    border-radius: 999px;

    font-size: 12px;

    font-weight: 800;

    background: #222;

}

.badge.online {

    color: var(--green);

    background: rgba(57,217,138,.1);
}

.badge.offline {

    color: var(--red);

    background: rgba(255,93,93,.1);
}

.badge.warn {

    color: var(--yellow);

    background: rgba(255,200,87,.1);
}

.reason-preview {

    color: #bbb;

    margin-top: 10px;

    word-break: break-word;
}

.timer {

    font-size: 32px;

    font-weight: 900;

    margin-top: 10px;
}

.save-status {

    min-height: 22px;

    margin-top: 12px;

    font-size: 13px;

}

.save-status.success {

    color: var(--green);
}

.save-status.error {

    color: var(--red);
}

.save-status.info {

    color: var(--blue);
}

.token-box {

    display: none;

    margin-top: 12px;

    padding: 12px;

    border-radius: 10px;

    background: #080808;

    border: 1px solid #333;

    word-break: break-all;

    font-family: monospace;

    color: #ddd;
}

.small {

    color: #777;

    font-size: 12px;

    margin-top: 8px;
}

.dirty {

    border-color: var(--yellow) !important;

}

@media (max-width: 700px) {

    .statusbar,
    .grid,
    .btn-row {

        grid-template-columns: 1fr;
    }

    .header {

        align-items: flex-start;

    }

}

</style>

</head>

<body>

<div class="container">

    <div class="header">

        <div>

            <div class="title">
                🎮 Roblox Control Center
            </div>

            <div class="subtitle">
                Stable Control Dashboard
            </div>

        </div>

        <a
            class="logout"
            href="/logout"
        >
            ออกจากระบบ
        </a>

    </div>


    <!-- SYSTEM STATUS -->

    <div class="statusbar">

        <div class="status">

            <div class="status-label">
                DATABASE
            </div>

            <div
                id="dbStatus"
                class="status-value"
            >
                กำลังตรวจสอบ...
            </div>

        </div>

        <div class="status">

            <div class="status-label">
                BACKUP
            </div>

            <div
                id="backupStatus"
                class="status-value"
            >
                กำลังตรวจสอบ...
            </div>

        </div>

        <div class="status">

            <div class="status-label">
                CURRENT MAP
            </div>

            <div
                id="currentStatus"
                class="status-value"
            >
                -
            </div>

        </div>

    </div>


    <!-- CONTROL -->

    <div class="card">

        <div class="section-title">
            ⚙️ ควบคุมเซิร์ฟเวอร์
        </div>


        <div class="field">

            <label>
                เลือกแมพ
            </label>

            <select
                id="mapSelect"
                onchange="loadSelected()"
            >

                {% for name, pid in maps.items() %}

                <option value="{{ pid }}">
                    {{ name }} — {{ pid }}
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
                autocomplete="off"
            >

            <div class="small">
                สูงสุด 500 ตัวอักษร
            </div>

        </div>


        <div class="grid">

            <div class="field">

                <label>
                    Countdown (วินาที)
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
                    โหมด Countdown
                </label>

                <div
                    style="
                        padding:13px 0;
                    "
                >

                    <input
                        id="timerEnabled"
                        type="checkbox"
                        checked
                        style="
                            width:auto;
                        "
                    >

                    เปิดใช้งาน Countdown

                </div>

            </div>

        </div>


        <!-- STATUS ACTIONS -->

        <div class="btn-row">

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
                class="btn close"
                onclick="actionSelected('closed')"
            >
                🚨 ปิดทันที
            </button>

        </div>


        <!-- SAVE CONFIG -->

        <button
            id="saveConfigButton"
            class="btn blue"
            style="
                width:100%;
                margin-top:12px;
            "
            onclick="saveConfig()"
        >
            💾 บันทึกข้อความ + เวลา
        </button>


        <div
            id="saveStatus"
            class="save-status"
        ></div>


        <!-- STATE -->

        <div class="state-box">

            <div class="state-main">

                <strong>
                    สถานะปัจจุบัน
                </strong>

                <span
                    id="stateBadge"
                    class="badge"
                >
                    -
                </span>

            </div>

            <div
                id="reasonPreview"
                class="reason-preview"
            >
                -
            </div>

            <div
                id="timerDisplay"
                class="timer"
            >
                -
            </div>

        </div>

    </div>


    <!-- ALL MAPS -->

    <div class="card">

        <div class="section-title">
            🌐 ควบคุมทุกแมพ
        </div>

        <div class="btn-row">

            <button
                class="btn close"
                onclick="actionAll('closed')"
            >
                🚨 ปิดทุกแมพทันที
            </button>

            <button
                class="btn cancel"
                onclick="actionAll('open')"
            >
                ↩️ เปิด / ยกเลิกทุกแมพ
            </button>

            <button
                class="btn blue"
                onclick="actionAll('scheduled')"
            >
                ⏳ Countdown ทุกแมพ
            </button>

        </div>

    </div>


    <!-- TOKEN -->

    <div class="card">

        <div class="section-title">
            🔐 System Token
        </div>

        <div class="field">

            <label>
                Token PIN
            </label>

            <input
                id="pinInput"
                type="password"
                placeholder="กรอก Token PIN"
                autocomplete="off"
            >

        </div>

        <div class="btn-row">

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
                🔄 สร้าง Token ใหม่
            </button>

        </div>

        <div
            id="tokenBox"
            class="token-box"
        ></div>

    </div>

</div>


<script>

/* ==========================================================
   Roblox Control Center Frontend
   ========================================================== */

const stateCache = {};

let selectedPlaceId = null;

let refreshTimer = null;

let inputDirty = false;

let isSaving = false;


/* ==========================================================
   DOM
   ========================================================== */

const mapSelect =
    document.getElementById("mapSelect");

const reasonInput =
    document.getElementById("reasonInput");

const secondsInput =
    document.getElementById("secondsInput");

const timerEnabled =
    document.getElementById("timerEnabled");

const saveStatus =
    document.getElementById("saveStatus");

const saveConfigButton =
    document.getElementById(
        "saveConfigButton"
    );


/* ==========================================================
   Input Dirty Tracking
   ========================================================== */

function markDirty() {

    inputDirty = true;

    reasonInput.classList.add("dirty");

    secondsInput.classList.add("dirty");
}


function clearDirty() {

    inputDirty = false;

    reasonInput.classList.remove("dirty");

    secondsInput.classList.remove("dirty");
}


reasonInput.addEventListener(
    "input",
    markDirty
);

secondsInput.addEventListener(
    "input",
    markDirty
);


/* ==========================================================
   Save Status
   ========================================================== */

function setSaveStatus(
    message,
    type = "info"
) {

    saveStatus.textContent =
        message;

    saveStatus.className =
        "save-status " + type;
}


/* ==========================================================
   API
   ========================================================== */

async function api(
    url,
    options = {}
) {

    const finalOptions = {
        cache: "no-store",
        ...options,
        headers: {
            "Content-Type":
                "application/json",
            ...(options.headers || {})
        }
    };

    const response =
        await fetch(
            url +
                (
                    url.includes("?")
                        ? "&"
                        : "?"
                ) +
                "_=" +
                Date.now(),
            finalOptions
        );

    let data = null;

    try {

        data =
            await response.json();

    } catch {

        throw new Error(
            "Server ส่งข้อมูลไม่ถูกต้อง"
        );

    }

    if (!response.ok || !data.ok) {

        throw new Error(
            data.error ||
            `HTTP ${response.status}`
        );

    }

    return data;
}


/* ==========================================================
   Format
   ========================================================== */

function formatSeconds(seconds) {

    seconds =
        Math.max(
            0,
            Math.floor(
                Number(seconds) || 0
            )
        );

    const h =
        Math.floor(
            seconds / 3600
        );

    const m =
        Math.floor(
            (seconds % 3600) / 60
        );

    const s =
        seconds % 60;

    if (h > 0) {

        return (
            String(h).padStart(2, "0")
            + ":" +
            String(m).padStart(2, "0")
            + ":" +
            String(s).padStart(2, "0")
        );

    }

    return (
        String(m).padStart(2, "0")
        + ":" +
        String(s).padStart(2, "0")
    );
}


/* ==========================================================
   Badge
   ========================================================== */

function updateBadge(state) {

    const badge =
        document.getElementById(
            "stateBadge"
        );

    if (state.mode === "open") {

        badge.textContent =
            "● OPEN";

        badge.className =
            "badge online";

    }

    else if (
        state.mode === "scheduled"
    ) {

        badge.textContent =
            "● COUNTDOWN";

        badge.className =
            "badge warn";

    }

    else {

        badge.textContent =
            "● CLOSED";

        badge.className =
            "badge offline";
    }
}


/* ==========================================================
   Render Selected State
   ========================================================== */

function renderSelected(
    state
) {

    if (!state) {
        return;
    }

    selectedPlaceId =
        mapSelect.value;

    updateBadge(state);

    document.getElementById(
        "reasonPreview"
    ).textContent =
        state.reason || "-";


    document.getElementById(
        "currentStatus"
    ).textContent =
        state.mode;


    /*
       IMPORTANT:

       ถ้าผู้ใช้กำลังพิมพ์อยู่
       ห้าม Auto Refresh เอาค่าเก่ามาทับ
    */

    if (!inputDirty && !isSaving) {

        reasonInput.value =
            state.reason || "";

        secondsInput.value =
            state.seconds || 60;
    }


    const timer =
        document.getElementById(
            "timerDisplay"
        );


    if (
        state.mode === "scheduled"
    ) {

        const remaining =
            Number(
                state.remaining || 0
            );

        timer.textContent =
            "เหลือ " +
            formatSeconds(
                remaining
            );

    }

    else if (
        state.mode === "closed"
    ) {

        timer.textContent =
            "เซิร์ฟเวอร์ปิดอยู่";

    }

    else {

        timer.textContent =
            "เซิร์ฟเวอร์เปิดอยู่";
    }
}


/* ==========================================================
   Load Status
   ========================================================== */

async function loadStatus() {

    try {

        const data =
            await api(
                "/api/status"
            );


        document.getElementById(
            "dbStatus"
        ).textContent =
            data.dbAvailable
                ? "🟢 Connected"
                : "🔴 Offline";


        document.getElementById(
            "backupStatus"
        ).textContent =
            data.backupAvailable
                ? "🟢 Ready"
                : "🟡 Unavailable";


        for (
            const state
            of data.states
        ) {

            stateCache[
                state.placeId
            ] = state;

        }


        loadSelected(
            false
        );

    }

    catch (error) {

        document.getElementById(
            "dbStatus"
        ).textContent =
            "🔴 Error";

        setSaveStatus(
            error.message,
            "error"
        );
    }
}


/* ==========================================================
   Load Selected
   ========================================================== */

function loadSelected(
    clearDirtyState = true
) {

    const pid =
        mapSelect.value;

    selectedPlaceId =
        pid;

    /*
       เมื่อเปลี่ยนแมพ
       ให้โหลดข้อมูลของแมพใหม่
       ไม่ใช่ข้อมูลเก่าของแมพเดิม
    */

    if (clearDirtyState) {

        clearDirty();

    }

    const state =
        stateCache[pid];

    if (state) {

        renderSelected(
            state
        );

    }
}


/* ==========================================================
   Save Config
   ========================================================== */

async function saveConfig() {

    if (isSaving) {
        return;
    }

    const placeId =
        mapSelect.value;

    const reason =
        reasonInput.value;

    const seconds =
        Number(
            secondsInput.value
        );


    if (
        !Number.isInteger(seconds)
        ||
        seconds < 1
        ||
        seconds > 86400
    ) {

        setSaveStatus(
            "เวลา Countdown ต้องอยู่ระหว่าง 1-86400 วินาที",
            "error"
        );

        return;
    }


    isSaving = true;

    saveConfigButton.disabled =
        true;

    setSaveStatus(
        "⏳ กำลังบันทึกและตรวจสอบกับ Supabase...",
        "info"
    );


    try {

        const data =
            await api(
                "/api/save-config",
                {
                    method: "POST",

                    body: JSON.stringify({
                        scope: "place",

                        place_id:
                            placeId,

                        reason:
                            reason,

                        seconds:
                            seconds
                    })
                }
            );


        /*
           Server ยืนยันแล้วว่า
           DB มีค่าตรงกับที่ส่ง
        */

        const savedState =
            data.state;


        stateCache[
            placeId
        ] = savedState;


        /*
           ตอนนี้ค่อยเอาค่า Server
           มาใส่กลับใน input
        */

        reasonInput.value =
            savedState.reason;

        secondsInput.value =
            savedState.seconds;


        clearDirty();


        renderSelected(
            savedState
        );


        setSaveStatus(
            "✅ บันทึกสำเร็จ และตรวจสอบข้อมูลใน Supabase แล้ว",
            "success"
        );

    }

    catch (error) {

        /*
           สำคัญ:
           ถ้า Save ไม่สำเร็จ
           ไม่เอาค่าเก่ามาทับช่องที่ผู้ใช้กำลังแก้
        */

        setSaveStatus(
            "❌ " + error.message,
            "error"
        );

    }

    finally {

        isSaving = false;

        saveConfigButton.disabled =
            false;
    }
}


/* ==========================================================
   Selected Action
   ========================================================== */

async function actionSelected(
    mode
) {

    const placeId =
        mapSelect.value;

    const reason =
        reasonInput.value;

    const seconds =
        Number(
            secondsInput.value
        );


    if (
        !Number.isInteger(seconds)
        ||
        seconds < 1
        ||
        seconds > 86400
    ) {

        setSaveStatus(
            "เวลา Countdown ไม่ถูกต้อง",
            "error"
        );

        return;
    }


    setSaveStatus(
        "⏳ กำลังเปลี่ยนสถานะ...",
        "info"
    );


    try {

        const data =
            await api(
                "/api/update",
                {
                    method: "POST",

                    body: JSON.stringify({
                        place_id:
                            placeId,

                        mode:
                            mode,

                        reason:
                            reason,

                        seconds:
                            seconds
                    })
                }
            );


        stateCache[
            placeId
        ] =
            data.state;


        reasonInput.value =
            data.state.reason;

        secondsInput.value =
            data.state.seconds;


        clearDirty();


        renderSelected(
            data.state
        );


        setSaveStatus(
            "✅ เปลี่ยนสถานะและบันทึกข้อมูลสำเร็จ",
            "success"
        );

    }

    catch (error) {

        setSaveStatus(
            "❌ " + error.message,
            "error"
        );
    }
}


/* ==========================================================
   All Actions
   ========================================================== */

async function actionAll(
    mode
) {

    const reason =
        reasonInput.value;

    const seconds =
        Number(
            secondsInput.value
        );


    if (
        !Number.isInteger(seconds)
        ||
        seconds < 1
        ||
        seconds > 86400
    ) {

        setSaveStatus(
            "เวลา Countdown ไม่ถูกต้อง",
            "error"
        );

        return;
    }


    setSaveStatus(
        "⏳ กำลังอัปเดตทุกแมพ...",
        "info"
    );


    try {

        const data =
            await api(
                "/api/update-all",
                {
                    method: "POST",

                    body: JSON.stringify({
                        mode:
                            mode,

                        reason:
                            reason,

                        seconds:
                            seconds
                    })
                }
            );


        let failed = 0;


        for (
            const result
            of data.results
        ) {

            if (
                result.ok
            ) {

                stateCache[
                    result.placeId
                ] =
                    result.state;

            }

            else {

                failed++;
            }
        }


        const current =
            stateCache[
                mapSelect.value
            ];


        if (current) {

            renderSelected(
                current
            );
        }


        clearDirty();


        if (failed === 0) {

            setSaveStatus(
                "✅ อัปเดตทุกแมพสำเร็จ",
                "success"
            );

        }

        else {

            setSaveStatus(
                "⚠️ สำเร็จบางแมพ แต่มี " +
                failed +
                " แมพที่ล้มเหลว",
                "error"
            );
        }

    }

    catch (error) {

        setSaveStatus(
            "❌ " + error.message,
            "error"
        );
    }
}


/* ==========================================================
   Reveal Token
   ========================================================== */

async function revealToken() {

    const pin =
        document.getElementById(
            "pinInput"
        ).value;


    if (!pin) {

        setSaveStatus(
            "กรุณากรอก Token PIN",
            "error"
        );

        return;
    }


    try {

        const data =
            await api(
                "/api/token/reveal",
                {
                    method: "POST",

                    body: JSON.stringify({
                        pin: pin
                    })
                }
            );


        const box =
            document.getElementById(
                "tokenBox"
            );


        box.textContent =
            data.token;

        box.style.display =
            "block";


        setSaveStatus(
            "✅ ยืนยัน PIN สำเร็จ",
            "success"
        );

    }

    catch (error) {

        setSaveStatus(
            "❌ " + error.message,
            "error"
        );
    }
}


/* ==========================================================
   New Token
   ========================================================== */

async function newToken() {

    const pin =
        document.getElementById(
            "pinInput"
        ).value;


    if (!pin) {

        setSaveStatus(
            "กรุณากรอก Token PIN",
            "error"
        );

        return;
    }


    if (
        !confirm(
            "ต้องการสร้าง Token ใหม่จริงหรือไม่?\n\nToken เดิมจะใช้งานไม่ได้"
        )
    ) {

        return;
    }


    try {

        const data =
            await api(
                "/api/token/regenerate",
                {
                    method: "POST",

                    body: JSON.stringify({
                        pin: pin
                    })
                }
            );


        const box =
            document.getElementById(
                "tokenBox"
            );


        box.textContent =
            data.token;

        box.style.display =
            "block";


        setSaveStatus(
            "✅ สร้าง Token ใหม่สำเร็จ",
            "success"
        );

    }

    catch (error) {

        setSaveStatus(
            "❌ " + error.message,
            "error"
        );
    }
}


/* ==========================================================
   Automatic Refresh
   ========================================================== */

refreshTimer =
    setInterval(
        async function() {

            /*
               ถ้ากำลัง Save
               ห้าม refresh มาแทรก
            */

            if (isSaving) {
                return;
            }

            await loadStatus();

        },
        2000
    );


/* ==========================================================
   Initial Load
   ========================================================== */

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

        "dbAvailable":
            DB_AVAILABLE,

        "backupAvailable":
            BACKUP_AVAILABLE,

        "lastDbSync":
            LAST_DB_SYNC,

        "lastDbError":
            LAST_DB_ERROR,

        "lastBackupSave":
            LAST_BACKUP_SAVE,

        "lastBackupError":
            LAST_BACKUP_ERROR,

        "initialized":
            INITIALIZED,

        "states":
            states,
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

        "serverNow":
            time.time(),

        "state":
            snapshot(
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
        "mode":
            state["mode"],

        "reason":
            state["reason"],

        "deadline":
            state["deadline"],

        "seconds":
            state["seconds"],

        "serverNow":
            state["serverNow"],

        "remaining":
            state["remaining"],

        "elapsed":
            state["elapsed"],
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
            "ok":
                result["success"],

            "results":
                result["results"],

            "failed":
                result["failed"],
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
#
# ใช้สำหรับ:
# - reason
# - seconds
#
# โดยไม่เปลี่ยน mode
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

            for pid in PLACE_IDS:

                with WRITE_LOCK:

                    with LOCK:

                        current = copy.deepcopy(
                            DATA[
                                "places"
                            ][pid]
                        )

                    new_state = clean_state({
                        **current,

                        "reason":
                            reason,

                        "seconds":
                            seconds,
                    })

                    # ----------------------------------------
                    # DB FIRST
                    # ----------------------------------------

                    ok, error, _ = (
                        save_with_retry(
                            pid,
                            new_state,
                        )
                    )

                    if not ok:

                        raise RuntimeError(
                            f"{pid}: "
                            f"{error}"
                        )

                    # ----------------------------------------
                    # Verify DB
                    # ----------------------------------------

                    verified, verify_error = (
                        verify_database_state(
                            pid,
                            new_state,
                        )
                    )

                    if not verified:

                        raise RuntimeError(
                            f"{pid}: "
                            f"ตรวจสอบหลังบันทึกไม่ผ่าน: "
                            f"{verify_error}"
                        )

                    # ----------------------------------------
                    # DB verified -> RAM
                    # ----------------------------------------

                    with LOCK:

                        DATA[
                            "places"
                        ][pid] = (
                            copy.deepcopy(
                                new_state
                            )
                        )

                results.append(
                    snapshot(pid)
                )

            # Backup only after all DB saves
            save_local_backup()

            return jsonify({
                "ok": True,

                "results":
                    results,
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
                    DATA[
                        "places"
                    ][place_id]
                )

            # ------------------------------------------------
            # สำคัญ:
            # เปลี่ยนเฉพาะ reason + seconds
            # mode / deadline ไม่แตะ
            # ------------------------------------------------

            new_state = clean_state({
                **current,

                "reason":
                    reason,

                "seconds":
                    seconds,
            })


            # ------------------------------------------------
            # DB FIRST
            # ------------------------------------------------

            ok, error, _ = (
                save_with_retry(
                    place_id,
                    new_state,
                )
            )


            if not ok:

                raise RuntimeError(
                    "บันทึกไม่สำเร็จ: "
                    + str(error)
                )


            # ------------------------------------------------
            # Verify DB
            # ------------------------------------------------

            verified, verify_error = (
                verify_database_state(
                    place_id,
                    new_state,
                )
            )


            if not verified:

                raise RuntimeError(
                    "บันทึกแล้ว แต่ข้อมูลใน "
                    "Supabase ไม่ตรงกับค่าที่ส่ง: "
                    + str(verify_error)
                )


            # ------------------------------------------------
            # DB verified -> RAM
            # ------------------------------------------------

            with LOCK:

                DATA[
                    "places"
                ][place_id] = (
                    copy.deepcopy(
                        new_state
                    )
                )


        # ----------------------------------------------------
        # Backup
        # ----------------------------------------------------

        backup_ok, backup_error = (
            save_local_backup()
        )

        if not backup_ok:

            LOG.warning(
                "DB saved but backup failed: %s",
                backup_error,
            )


        # ----------------------------------------------------
        # Return verified state
        # ----------------------------------------------------

        final_state = snapshot(
            place_id
        )


        return jsonify({
            "ok": True,

            "state":
                final_state,

            "databaseVerified":
                True,

            "backupAvailable":
                BACKUP_AVAILABLE,
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


        return jsonify({
            "ok": True,

            "token":
                token,
        })


    except Exception as exc:

        LOG.error(
            "api_token_reveal failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,

            "error":
                str(exc),
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


        with WRITE_LOCK:

            # ------------------------------------------------
            # Save FIRST
            # ------------------------------------------------

            ok, error, _ = (
                save_with_retry(
                    SYSTEM_TOKEN_ID,
                    {
                        "token":
                            new_token,
                    },
                )
            )


            if not ok:

                raise RuntimeError(
                    "บันทึก Token ใหม่ใน Supabase "
                    "ไม่สำเร็จ: "
                    + str(error)
                )


            # ------------------------------------------------
            # Verify
            # ------------------------------------------------

            verified, verify_error = (
                verify_database_state(
                    SYSTEM_TOKEN_ID,
                    {
                        "token":
                            new_token
                    },
                )
            )


            if not verified:

                raise RuntimeError(
                    "Token ถูกบันทึกแต่ "
                    "ตรวจสอบไม่ผ่าน: "
                    + str(verify_error)
                )


            # ------------------------------------------------
            # DB verified -> RAM
            # ------------------------------------------------

            with LOCK:

                DATA["token"] = (
                    new_token
                )


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

            "token":
                new_token,

            "databaseVerified":
                True,
        })


    except Exception as exc:

        LOG.error(
            "api_token_regenerate failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,

            "error":
                str(exc),
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
