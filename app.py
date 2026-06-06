import os
import re
import json
import uuid
import mimetypes
import threading
import queue as _pyqueue
from collections import deque
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify, render_template, redirect, g, has_request_context
from dotenv import load_dotenv

import gspread
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from io import BytesIO


load_dotenv()


# =========================================================
# Flask
# =========================================================

app = Flask(__name__)
# จำกัดขนาดไฟล์อัปโหลดผ่าน LIFF ไม่เกิน 100 MB
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


@app.errorhandler(413)
def file_too_large(e):
    max_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024)
    return jsonify({
        "success": False,
        "message": f"ไฟล์ใหญ่เกิน {max_mb} MB กรุณาอัปโหลดไฟล์ไป Google Drive แล้วส่งเป็นลิงก์แทน",
    }), 413


SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "20") or "20")
SHEET_API_RETRY_COUNT = int(os.getenv("SHEET_API_RETRY_COUNT", "4") or "4")
_sheet_shared_cache = {}
_sheet_shared_cache_lock = threading.Lock()
_operation_locks = {}
_operation_locks_lock = threading.Lock()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_db_initialized = False
_db_init_lock = threading.Lock()

# Rate limit / queuing for Sheets API to avoid 429 write quota errors
_sheets_rate_limit_queue_env = str(os.getenv("SHEETS_RATE_LIMIT_QUEUE", "1") or "1").strip().lower()
SHEETS_RATE_LIMIT_QUEUE = _sheets_rate_limit_queue_env in {"1", "true", "yes", "on"}
SHEETS_MAX_CALLS_PER_MINUTE = int(os.getenv("SHEETS_MAX_CALLS_PER_MINUTE", "60") or "60")
_sheets_request_queue = _pyqueue.Queue()
_sheets_worker_thread = None
_sheets_worker_started = False


def _sheets_queue_worker():
    """Worker that processes Sheets API calls from a queue at a limited rate.

    Each item is a tuple (fn, result_container, done_event).
    """
    interval = 60.0 / max(1, SHEETS_MAX_CALLS_PER_MINUTE)
    while True:
        try:
            fn, container, ev = _sheets_request_queue.get()
        except Exception:
            time.sleep(1)
            continue

        try:
            res = _call_google_sheet_api_impl(fn)
            container["result"] = res
        except Exception as e:
            container["exception"] = e
        finally:
            try:
                ev.set()
            except Exception:
                pass

        time.sleep(interval)


def _ensure_sheets_worker():
    global _sheets_worker_thread, _sheets_worker_started
    if _sheets_worker_started:
        return
    _sheets_worker_thread = threading.Thread(target=_sheets_queue_worker, daemon=True)
    _sheets_worker_thread.start()
    _sheets_worker_started = True


def is_google_quota_error(error):
    status_code = (
        getattr(getattr(error, "response", None), "status_code", None)
        or getattr(getattr(error, "resp", None), "status", None)
    )
    if status_code in {429, 500, 502, 503, 504}:
        return True

    text = str(error).lower()
    return (
        "quota exceeded" in text
        or "rate limit" in text
        or "429" in text
        or "500" in text
        or "502" in text
        or "503" in text
        or "504" in text
    )


def _call_google_sheet_api_impl(fn):
    delay = 0.7
    last_error = None
    for attempt in range(SHEET_API_RETRY_COUNT):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if not is_google_quota_error(e) or attempt == SHEET_API_RETRY_COUNT - 1:
                raise
            time.sleep(delay)
            delay *= 1.8
    raise last_error


def call_google_sheet_api(fn):
    """Public wrapper for Google Sheets API calls.

    When `SHEETS_RATE_LIMIT_QUEUE` is enabled, requests are enqueued and processed
    by a background worker at a limited rate to avoid 429 write quota errors.
    This function waits synchronously for the worker to process the request and
    returns the result (or raises the exception).
    """
    if not SHEETS_RATE_LIMIT_QUEUE:
        return _call_google_sheet_api_impl(fn)

    # ensure worker running
    _ensure_sheets_worker()

    container = {}
    ev = threading.Event()
    _sheets_request_queue.put((fn, container, ev))
    ev.wait()
    if "exception" in container:
        raise container["exception"]
    return container.get("result")


def clone_sheet_rows(rows):
    return [dict(row) for row in (rows or [])]


def invalidate_shared_sheet_cache(sheet_name):
    with _sheet_shared_cache_lock:
        for key in [
            ("records", sheet_name),
            ("headers", sheet_name),
        ]:
            _sheet_shared_cache.pop(key, None)


def get_shared_sheet_cache(cache_type, sheet_name, loader, clone_value=None):
    now = time.monotonic()
    key = (cache_type, sheet_name)

    with _sheet_shared_cache_lock:
        cached = _sheet_shared_cache.get(key)
        if cached and now - cached["loaded_at"] < SHEET_CACHE_TTL_SECONDS:
            value = cached["value"]
            return clone_value(value) if clone_value else value

        value = call_google_sheet_api(loader)
        _sheet_shared_cache[key] = {
            "loaded_at": time.monotonic(),
            "value": value,
        }
        return clone_value(value) if clone_value else value


def get_operation_lock(lock_key):
    with _operation_locks_lock:
        lock = _operation_locks.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _operation_locks[lock_key] = lock
        return lock


def db_enabled():
    return bool(DATABASE_URL)


def get_db_connection():
    import psycopg2

    database_url = DATABASE_URL
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    return psycopg2.connect(database_url)


def ensure_db_tables():
    global _db_initialized
    if not db_enabled() or _db_initialized:
        return

    with _db_init_lock:
        if _db_initialized:
            return

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS student_registrations (
                        student_line_user_id TEXT PRIMARY KEY,
                        student_name TEXT NOT NULL,
                        student_code TEXT NOT NULL,
                        classroom TEXT NOT NULL,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        sheets_status TEXT NOT NULL DEFAULT 'pending',
                        sheets_error TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS student_submissions (
                        student_line_user_id TEXT NOT NULL,
                        assignment_id TEXT NOT NULL,
                        assignment_title TEXT NOT NULL DEFAULT '',
                        classroom TEXT NOT NULL DEFAULT '',
                        file_url TEXT NOT NULL DEFAULT '',
                        file_name TEXT NOT NULL DEFAULT '',
                        note TEXT NOT NULL DEFAULT '',
                        late TEXT NOT NULL DEFAULT '',
                        auto_score TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        sheets_status TEXT NOT NULL DEFAULT 'pending',
                        sheets_error TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (student_line_user_id, assignment_id)
                    )
                """)
        _db_initialized = True


def db_json(value):
    from psycopg2.extras import Json

    return Json(value or {})


def save_registration_to_db(student_line_user_id, student_name, student_code, classroom):
    if not db_enabled():
        return False

    try:
        ensure_db_tables()
        payload = {
            "student_line_user_id": student_line_user_id,
            "student_name": student_name,
            "student_code": student_code,
            "classroom": classroom,
        }
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO student_registrations (
                        student_line_user_id, student_name, student_code, classroom, payload, sheets_status, sheets_error
                    )
                    VALUES (%s, %s, %s, %s, %s, 'pending', '')
                    ON CONFLICT (student_line_user_id) DO UPDATE SET
                        student_name = EXCLUDED.student_name,
                        student_code = EXCLUDED.student_code,
                        classroom = EXCLUDED.classroom,
                        payload = EXCLUDED.payload,
                        sheets_status = 'pending',
                        sheets_error = '',
                        updated_at = NOW()
                """, (
                    student_line_user_id,
                    student_name,
                    student_code,
                    classroom,
                    db_json(payload),
                ))
        return True
    except Exception as e:
        print("[save_registration_to_db] Error:", e)
        return False


def save_submission_to_db(data):
    if not db_enabled():
        return False

    try:
        ensure_db_tables()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO student_submissions (
                        student_line_user_id, assignment_id, assignment_title, classroom,
                        file_url, file_name, note, late, auto_score, payload, sheets_status, sheets_error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', '')
                    ON CONFLICT (student_line_user_id, assignment_id) DO UPDATE SET
                        assignment_title = EXCLUDED.assignment_title,
                        classroom = EXCLUDED.classroom,
                        file_url = EXCLUDED.file_url,
                        file_name = EXCLUDED.file_name,
                        note = EXCLUDED.note,
                        late = EXCLUDED.late,
                        auto_score = EXCLUDED.auto_score,
                        payload = EXCLUDED.payload,
                        sheets_status = 'pending',
                        sheets_error = '',
                        updated_at = NOW()
                """, (
                    data.get("student_line_user_id", ""),
                    data.get("assignment_id", ""),
                    data.get("assignment_title", ""),
                    data.get("classroom", ""),
                    data.get("file_url", ""),
                    data.get("file_name", ""),
                    data.get("note", ""),
                    data.get("late", ""),
                    data.get("auto_score", ""),
                    db_json(data),
                ))
        return True
    except Exception as e:
        print("[save_submission_to_db] Error:", e)
        return False


def mark_db_sheets_status(table_name, key_values, status, error=""):
    if not db_enabled():
        return

    try:
        ensure_db_tables()
        if table_name == "student_registrations":
            where_sql = "student_line_user_id = %s"
            params = [key_values.get("student_line_user_id", "")]
        elif table_name == "student_submissions":
            where_sql = "student_line_user_id = %s AND assignment_id = %s"
            params = [
                key_values.get("student_line_user_id", ""),
                key_values.get("assignment_id", ""),
            ]
        else:
            return

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {table_name}
                    SET sheets_status = %s, sheets_error = %s, updated_at = NOW()
                    WHERE {where_sql}
                    """,
                    [status, str(error or "")[:1000], *params],
                )
    except Exception as e:
        print("[mark_db_sheets_status] Error:", e)


def get_registration_from_db(student_line_user_id):
    if not db_enabled():
        return None

    try:
        ensure_db_tables()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT student_line_user_id, student_name, student_code, classroom
                    FROM student_registrations
                    WHERE student_line_user_id = %s
                """, (str(student_line_user_id or "").strip(),))
                row = cur.fetchone()
        if not row:
            return None
        return {
            "student_line_user_id": row[0],
            "student_name": row[1],
            "student_code": row[2],
            "classroom": row[3],
        }
    except Exception as e:
        print("[get_registration_from_db] Error:", e)
        return None


def get_submission_from_db(student_line_user_id, assignment_id):
    if not db_enabled():
        return None

    try:
        ensure_db_tables()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT payload
                    FROM student_submissions
                    WHERE student_line_user_id = %s AND assignment_id = %s
                """, (
                    str(student_line_user_id or "").strip(),
                    str(assignment_id or "").strip(),
                ))
                row = cur.fetchone()
        return dict(row[0] or {}) if row else None
    except Exception as e:
        print("[get_submission_from_db] Error:", e)
        return None


def get_submissions_from_db(student_line_user_id):
    if not db_enabled():
        return []

    try:
        ensure_db_tables()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT payload
                    FROM student_submissions
                    WHERE student_line_user_id = %s
                    ORDER BY updated_at DESC
                """, (str(student_line_user_id or "").strip(),))
                rows = cur.fetchall()
        return [dict(row[0] or {}) for row in rows]
    except Exception as e:
        print("[get_submissions_from_db] Error:", e)
        return []


def fetch_pending_db_rows(table_name, limit=50):
    if not db_enabled():
        return []

    try:
        ensure_db_tables()
        limit = max(1, min(int(limit or 50), 200))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if table_name == "student_registrations":
                    cur.execute("""
                        SELECT student_line_user_id, student_name, student_code, classroom, created_at
                        FROM student_registrations
                        WHERE sheets_status IN ('pending', 'sheet_failed')
                        ORDER BY updated_at ASC
                        LIMIT %s
                    """, (limit,))
                elif table_name == "student_submissions":
                    cur.execute("""
                        SELECT payload
                        FROM student_submissions
                        WHERE sheets_status IN ('pending', 'sheet_failed')
                        ORDER BY updated_at ASC
                        LIMIT %s
                    """, (limit,))
                else:
                    return []
                rows = cur.fetchall()
        if table_name == "student_registrations":
            return [
                {
                    "student_line_user_id": row[0],
                    "student_name": row[1],
                    "student_code": row[2],
                    "classroom": row[3],
                    "created_at": row[4],
                }
                for row in rows
            ]
        return [dict(row[0] or {}) for row in rows]
    except Exception as e:
        print("[fetch_pending_db_rows] Error:", e)
        return []


# =========================================================
# Timezone
# =========================================================

TZ = ZoneInfo("Asia/Bangkok")


def now_dt():
    return datetime.now(TZ)


def now_text():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def today_date():
    return now_dt().date()


# =========================================================
# ENV Helper
# =========================================================

def clean_liff_id(value):
    """
    รับได้ทั้ง LIFF ID ตรง ๆ และ URL แบบ https://liff.line.me/xxxx
    แล้วคืนค่าเป็น LIFF ID เท่านั้น
    """
    return str(value or "").strip().replace("https://liff.line.me/", "").replace("http://liff.line.me/", "")


def liff_url(liff_id):
    liff_id = clean_liff_id(liff_id)
    return f"https://liff.line.me/{liff_id}" if liff_id else ""


def student_register_prompt_text():
    register_url = liff_url(LIFF_STUDENT_REGISTER_ID)
    if register_url:
        return (
            "กรุณาลงทะเบียนนักเรียนก่อนใช้งาน\n\n"
            f"เปิดหน้าลงทะเบียน:\n{register_url}\n\n"
            "ถ้า Rich Menu ยังไม่ขึ้น ให้ปิดคีย์บอร์ดหรือพิมพ์ 'เมนู' อีกครั้ง"
        )

    return (
        "กรุณาลงทะเบียนนักเรียนก่อนใช้งาน\n\n"
        "ระบบยังไม่ได้ตั้งค่า LIFF_STUDENT_REGISTER_ID"
    )


def normalize_rooms_text(rooms):
    """
    รับห้องได้หลายรูปแบบ เช่น 401,402, 401 402, 4/1 หรือ ม.4/1
    แล้วคืน list ห้องแบบไม่ซ้ำ เช่น ["401", "402"]
    """
    rooms = str(rooms or "").strip()
    pattern = re.compile(
        r"(?<!\d)([1-6]\d{2})(?!\d)"
        r"|(?:[มป]\.?\s*)?([1-6])\s*[/.-]\s*(\d{1,2})"
    )

    result = []
    for match in pattern.finditer(rooms):
        if match.group(1):
            room = match.group(1)
        else:
            room = f"{match.group(2)}{int(match.group(3)):02d}"

        if room not in result:
            result.append(room)

    # กันเคส Google Sheets แปลง "401,402,403" เป็นเลข 401402403
    # แล้ว gspread อ่านกลับมาเป็นตัวเลขยาวติดกัน
    compact_digits = re.sub(r"\D+", "", rooms)
    if not result and compact_digits and len(compact_digits) % 3 == 0:
        for i in range(0, len(compact_digits), 3):
            room = compact_digits[i:i + 3]
            if re.fullmatch(r"[1-6]\d{2}", room) and room not in result:
                result.append(room)

    return result


def normalize_classroom_text(classroom):
    rooms = normalize_rooms_text(classroom)
    if rooms:
        return rooms[0]
    return str(classroom or "").strip()


def should_redirect_to_liff():
    """
    redirect ไป LIFF เฉพาะมือถือที่เปิดลิงก์เว็บตรง
    """
    if request.args.get("liff.state"):
        return False

    if request.args.get("code") or request.args.get("state"):
        return False

    ua = request.headers.get("User-Agent", "").lower()

    if "line" in ua:
        return False

    mobile_keywords = ["iphone", "ipad", "android", "mobile"]
    return any(k in ua for k in mobile_keywords)


def render_liff_template(template_name, liff_id):
    liff_id = clean_liff_id(liff_id)
    if liff_id and should_redirect_to_liff() and not request.args.get("preview"):
        return redirect(liff_url(liff_id), code=302)
    return render_template(template_name, liff_id=liff_id)


def require_debug_secret():
    """
    เปิด debug endpoint เฉพาะเมื่อใส่ ?secret=ค่าเดียวกับ CRON_SECRET
    """
    secret = request.args.get("secret", "").strip()
    return bool(CRON_SECRET) and secret == CRON_SECRET


# =========================================================
# ENV
# =========================================================

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

# รองรับทั้งชื่อใหม่/ชื่อเก่า กันใส่ ENV ผิดชื่อ
GOOGLE_DRIVE_ROOT_FOLDER_ID = (
    os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "").strip()
    or os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
)

CRON_SECRET = os.getenv("CRON_SECRET", "")
TEACHER_SETUP_CODE = os.getenv("TEACHER_SETUP_CODE", "")

# LIFF นักเรียน
LIFF_STUDENT_REGISTER_ID = clean_liff_id(os.getenv("LIFF_STUDENT_REGISTER_ID", ""))
LIFF_STUDENT_SUBMIT_ID = clean_liff_id(os.getenv("LIFF_STUDENT_SUBMIT_ID", ""))
LIFF_STUDENT_PENDING_ID = clean_liff_id(os.getenv("LIFF_STUDENT_PENDING_ID", ""))
LIFF_STUDENT_QUESTION_ID = clean_liff_id(os.getenv("LIFF_STUDENT_QUESTION_ID", ""))
LIFF_STUDENT_ANNOUNCE_ID = clean_liff_id(os.getenv("LIFF_STUDENT_ANNOUNCE_ID", ""))

# LIFF ครู
LIFF_TEACHER_SETUP_ID = clean_liff_id(os.getenv("LIFF_TEACHER_SETUP_ID", ""))
LIFF_TEACHER_ASSIGNMENT_ID = clean_liff_id(os.getenv("LIFF_TEACHER_ASSIGNMENT_ID", ""))
LIFF_TEACHER_PENDING_ID = clean_liff_id(os.getenv("LIFF_TEACHER_PENDING_ID", ""))
LIFF_TEACHER_QUESTIONS_ID = clean_liff_id(os.getenv("LIFF_TEACHER_QUESTIONS_ID", ""))
LIFF_TEACHER_ANNOUNCE_ID = clean_liff_id(os.getenv("LIFF_TEACHER_ANNOUNCE_ID", ""))

# Rich Menu นักเรียน
STUDENT_RICH_MENU_REGISTER_ID = os.getenv("STUDENT_RICH_MENU_REGISTER_ID", "")
STUDENT_RICH_MENU_NORMAL_ID = os.getenv("STUDENT_RICH_MENU_NORMAL_ID", "")
STUDENT_RICH_MENU_PENDING_ALERT_ID = os.getenv("STUDENT_RICH_MENU_PENDING_ALERT_ID", "")
STUDENT_RICH_MENU_ANSWER_ALERT_ID = os.getenv("STUDENT_RICH_MENU_ANSWER_ALERT_ID", "")
STUDENT_RICH_MENU_BOTH_ALERT_ID = os.getenv("STUDENT_RICH_MENU_BOTH_ALERT_ID", "")

# Rich Menu ครู
TEACHER_RICH_MENU_SETUP_ID = os.getenv("TEACHER_RICH_MENU_SETUP_ID", "")
TEACHER_RICH_MENU_NORMAL_ID = os.getenv("TEACHER_RICH_MENU_NORMAL_ID", "")
TEACHER_RICH_MENU_QUESTION_ALERT_ID = os.getenv("TEACHER_RICH_MENU_QUESTION_ALERT_ID", "")



# =========================================================
# Google Auth
# =========================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_google_credentials():
    """
    ใช้ OAuth บัญชี Google จริงก่อน ถ้าตั้งค่าไว้ครบ
    เพื่อให้อัปโหลดไฟล์ลง My Drive โดยใช้ quota ของบัญชีนั้น

    ถ้าไม่ได้ตั้ง OAuth จะ fallback เป็น service account แบบเดิม:
    1. GOOGLE_SERVICE_ACCOUNT_JSON
    2. GOOGLE_CREDENTIALS_JSON
    3. credentials.json ในโปรเจกต์
    """
    oauth_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    oauth_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    oauth_refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()

    if oauth_client_id and oauth_client_secret and oauth_refresh_token:
        token_uri = os.getenv("GOOGLE_OAUTH_TOKEN_URI", "").strip() or "https://oauth2.googleapis.com/token"
        creds = OAuthCredentials(
            token=None,
            refresh_token=oauth_refresh_token,
            token_uri=token_uri,
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            scopes=SCOPES,
        )
        creds.refresh(GoogleAuthRequest())
        return creds

    service_account_json = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        or os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    )

    if service_account_json:
        info = json.loads(service_account_json)
        return ServiceAccountCredentials.from_service_account_info(info, scopes=SCOPES)

    return ServiceAccountCredentials.from_service_account_file("credentials.json", scopes=SCOPES)


def get_gspread_client():
    if has_request_context():
        if not hasattr(g, "_gspread_client"):
            creds = get_google_credentials()
            g._gspread_client = gspread.authorize(creds)
        return g._gspread_client

    creds = get_google_credentials()
    return gspread.authorize(creds)


def get_spreadsheet():
    if has_request_context() and hasattr(g, "_spreadsheet"):
        return g._spreadsheet

    gc = get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    if has_request_context():
        g._spreadsheet = sh
    return sh


def get_worksheet(sheet_name):
    if has_request_context():
        worksheets = getattr(g, "_worksheets", None)
        if worksheets is None:
            worksheets = {}
            g._worksheets = worksheets
        if sheet_name in worksheets:
            return worksheets[sheet_name]

    sh = get_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=50)
    if has_request_context():
        g._worksheets[sheet_name] = ws
    return ws


# ------------------ Batch append buffer for Google Sheets ------------------
# Reduce number of write requests by buffering append_row calls per sheet
_sheet_append_buffers = {}
_sheet_append_buffers_lock = threading.Lock()
_batch_worker_thread = None
_batch_worker_started = False
BATCH_SIZE = int(os.getenv("SHEETS_BATCH_SIZE", "50") or "50")
FLUSH_INTERVAL = float(os.getenv("SHEETS_FLUSH_INTERVAL_SECONDS", "2") or "2")


def _ensure_batch_worker():
    global _batch_worker_thread, _batch_worker_started
    if _batch_worker_started:
        return
    _batch_worker_thread = threading.Thread(target=_batch_flusher, daemon=True)
    _batch_worker_thread.start()
    _batch_worker_started = True


def buffered_append_row(ws_or_name, row_values, value_input_option="USER_ENTERED"):
    """Buffer a row to append to the given sheet (worksheet object or sheet name).

    Flushes immediately if buffer reaches BATCH_SIZE. Returns True when queued.
    """
    try:
        sheet_name = getattr(ws_or_name, "title", None) or str(ws_or_name)
    except Exception:
        sheet_name = str(ws_or_name)

    with _sheet_append_buffers_lock:
        buf = _sheet_append_buffers.get(sheet_name)
        if buf is None:
            buf = {"rows": [], "value_input_option": value_input_option}
            _sheet_append_buffers[sheet_name] = buf
        buf["rows"].append(row_values)

        # immediate flush if exceed batch size
        if len(buf["rows"]) >= BATCH_SIZE:
            rows = buf["rows"][:BATCH_SIZE]
            del buf["rows"][:BATCH_SIZE]
            # perform flush synchronously
            try:
                sh = get_spreadsheet()
                ws = get_worksheet(sheet_name)
                call_google_sheet_api(lambda: ws.append_rows(rows, value_input_option=value_input_option))
            except Exception as e:
                print(f"[buffered_append_row] flush error for {sheet_name}:", e)

    # ensure background flusher running
    _ensure_batch_worker()
    return True


def _batch_flusher():
    while True:
        time.sleep(FLUSH_INTERVAL)
        to_flush = []
        with _sheet_append_buffers_lock:
            for sheet_name, buf in list(_sheet_append_buffers.items()):
                if buf["rows"]:
                    rows = buf["rows"][:]
                    buf["rows"] = []
                    to_flush.append((sheet_name, rows, buf.get("value_input_option", "USER_ENTERED")))

        for sheet_name, rows, value_input_option in to_flush:
            try:
                ws = get_worksheet(sheet_name)
                call_google_sheet_api(lambda: ws.append_rows(rows, value_input_option=value_input_option))
            except Exception as e:
                print(f"[batch_flusher] Failed to flush {len(rows)} rows to {sheet_name}:", e)


def invalidate_sheet_cache(sheet_name):
    invalidate_shared_sheet_cache(sheet_name)

    if not has_request_context():
        return

    for attr in ["_sheet_records", "_sheet_headers"]:
        cache = getattr(g, attr, None)
        if cache is not None:
            cache.pop(sheet_name, None)


def get_sheet_records(sheet_name):
    if has_request_context():
        records_cache = getattr(g, "_sheet_records", None)
        if records_cache is None:
            records_cache = {}
            g._sheet_records = records_cache
        if sheet_name not in records_cache:
            records_cache[sheet_name] = get_shared_sheet_cache(
                "records",
                sheet_name,
                lambda: get_worksheet(sheet_name).get_all_records(),
                clone_sheet_rows,
            )
        return records_cache[sheet_name]

    return get_shared_sheet_cache(
        "records",
        sheet_name,
        lambda: get_worksheet(sheet_name).get_all_records(),
        clone_sheet_rows,
    )


def get_sheet_headers(sheet_name):
    if has_request_context():
        headers_cache = getattr(g, "_sheet_headers", None)
        if headers_cache is None:
            headers_cache = {}
            g._sheet_headers = headers_cache
        if sheet_name not in headers_cache:
            headers_cache[sheet_name] = get_shared_sheet_cache(
                "headers",
                sheet_name,
                lambda: get_worksheet(sheet_name).row_values(1),
                list,
            )
        return headers_cache[sheet_name]

    return get_shared_sheet_cache(
        "headers",
        sheet_name,
        lambda: get_worksheet(sheet_name).row_values(1),
        list,
    )


def get_drive_service():
    creds = get_google_credentials()
    return build("drive", "v3", credentials=creds)


# =========================================================
# LINE API
# =========================================================

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_RICH_MENU_LINK_URL = "https://api.line.me/v2/bot/user/{user_id}/richmenu/{rich_menu_id}"
LINE_RICH_MENU_UNLINK_URL = "https://api.line.me/v2/bot/user/{user_id}/richmenu"


def line_headers():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def reply_message(reply_token, text):
    if not reply_token:
        return

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }

    try:
        r = requests.post(LINE_REPLY_URL, headers=line_headers(), json=payload, timeout=15)
        print("[reply_message]", r.status_code, r.text)
    except Exception as e:
        print("[reply_message] Error:", e)


def reply_messages(reply_token, messages):
    if not reply_token:
        return

    payload = {
        "replyToken": reply_token,
        "messages": messages,
    }

    try:
        r = requests.post(LINE_REPLY_URL, headers=line_headers(), json=payload, timeout=15)
        print("[reply_messages]", r.status_code, r.text)
    except Exception as e:
        print("[reply_messages] Error:", e)


def push_message(to, text, enable_notification=True):
    if not to:
        return

    message = {
        "type": "text",
        "text": text,
    }

    if not enable_notification:
        message["notificationDisabled"] = True

    payload = {
        "to": to,
        "messages": [message],
    }

    try:
        r = requests.post(LINE_PUSH_URL, headers=line_headers(), json=payload, timeout=15)
        print("[push_message]", r.status_code, r.text)
        return r
    except Exception as e:
        print("[push_message] Error:", e)
        return None


def push_messages(to, messages):
    if not to:
        return

    payload = {
        "to": to,
        "messages": messages,
    }

    try:
        r = requests.post(LINE_PUSH_URL, headers=line_headers(), json=payload, timeout=15)
        print("[push_messages]", r.status_code, r.text)
        return r
    except Exception as e:
        print("[push_messages] Error:", e)
        return None


def link_rich_menu_to_user(user_id, rich_menu_id):
    if not user_id or not rich_menu_id:
        return None

    url = LINE_RICH_MENU_LINK_URL.format(
        user_id=user_id,
        rich_menu_id=rich_menu_id,
    )

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    try:
        r = requests.post(url, headers=headers, timeout=15)
        print("[link_rich_menu_to_user]", r.status_code, r.text)
        return r
    except Exception as e:
        print("[link_rich_menu_to_user] Error:", e)
        return None


def line_response_summary(res):
    if not res:
        return None
    return {
        "status": res.status_code,
        "text": res.text,
    }


def unlink_rich_menu_from_user(user_id):
    if not user_id:
        return

    url = LINE_RICH_MENU_UNLINK_URL.format(user_id=user_id)

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    try:
        r = requests.delete(url, headers=headers, timeout=15)
        print("[unlink_rich_menu_from_user]", r.status_code, r.text)
    except Exception as e:
        print("[unlink_rich_menu_from_user] Error:", e)


# =========================================================
# Sheet Setup
# =========================================================

BASE_SHEETS = {
    "students": [
        "student_line_user_id",
        "line_user_ids",
        "student_name",
        "student_code",
        "classroom",
        "created_at",
    ],
    "teachers": [
        "teacher_line_user_id",
        "teacher_name",
        "rooms",
        "created_at",
    ],
    "assignments": [
        "assignment_id",
        "created_at",
        "teacher_line_user_id",
        "teacher_name",
        "classroom",
        "chapter_name",
        "title",
        "description",
        "start_date",
        "due_date",
        "due_time",
        "max_score",
        "score_category",
        "score_weight",
        "show_score_to_students",
        "allowed_file_types",
        "allow_link_submission",
    ],
    "submissions": [
        "submission_id",
        "submitted_at",
        "assignment_id",
        "assignment_title",
        "student_line_user_id",
        "student_name",
        "student_code",
        "classroom",
        "file_url",
        "file_name",
        "note",
        "late",
        "auto_score",
        "score",
        "checked_at",
        "checked_by",
        "teacher_comment",
    ],
    "questions": [
        "question_id",
        "created_at",
        "student_line_user_id",
        "student_name",
        "classroom",
        "question_text",
        "attachment_url",
        "attachment_name",
        "status",
        "answer_text",
        "answer_attachment_url",
        "answer_attachment_name",
        "answered_at",
        "answered_by",
        "student_seen",
        "is_pinned",
        "pinned_at",
        "pinned_by",
        "pinned_classrooms",
    ],
    "announcements": [
        "announcement_id",
        "created_at",
        "teacher_line_user_id",
        "teacher_name",
        "classroom",
        "message",
    ],
    "attendance": [
        "attendance_id",
        "attendance_date",
        "start_time",
        "end_time",
        "classroom",
        "student_line_user_id",
        "student_name",
        "student_code",
        "status",
        "note",
        "checked_at",
        "checked_by",
    ],
    "exam_scores": [
        "exam_score_id",
        "classroom",
        "student_line_user_id",
        "student_name",
        "student_code",
        "midterm_score",
        "final_score",
        "updated_at",
        "updated_by",
    ],
    "score_visibility": [
        "classroom",
        "show_midterm_scores",
        "show_final_scores",
        "show_work_scores",
        "show_exam_scores",
        "updated_at",
        "updated_by",
    ],
    "class_groups": [
        "classroom",
        "group_id",
        "updated_at",
    ],
    "deadline_logs": [
        "log_id",
        "created_at",
        "assignment_id",
        "classroom",
        "group_id",
        "notification_type",
        "message",
    ],
    "dirty_rooms": [
        "classroom",
        "reason",
        "updated_at",
    ],
}


FILE_TYPE_GROUPS = [
    {
        "id": "image",
        "exts": ["jpg", "jpeg", "png", "heic", "heif"],
        "label": "รูปภาพ JPG, JPEG, PNG, HEIC, HEIF",
    },
    {
        "id": "pdf",
        "exts": ["pdf"],
        "label": "PDF",
    },
    {
        "id": "word",
        "exts": ["doc", "docx"],
        "label": "Word DOC, DOCX",
    },
    {
        "id": "excel",
        "exts": ["xls", "xlsx"],
        "label": "Excel XLS, XLSX",
    },
    {
        "id": "powerpoint",
        "exts": ["ppt", "pptx"],
        "label": "PowerPoint PPT, PPTX",
    },
    {
        "id": "video",
        "exts": ["mp4", "mov"],
        "label": "วิดีโอ MP4, MOV",
    },
    {
        "id": "archive",
        "exts": ["zip", "rar"],
        "label": "ไฟล์บีบอัด ZIP, RAR",
    },
]

DEFAULT_ALLOWED_FILE_EXTS = []
FILE_TYPE_ALIASES = {}
for group in FILE_TYPE_GROUPS:
    group_exts = group["exts"]
    FILE_TYPE_ALIASES[group["id"]] = group_exts
    for ext in group_exts:
        if ext not in DEFAULT_ALLOWED_FILE_EXTS:
            DEFAULT_ALLOWED_FILE_EXTS.append(ext)
        FILE_TYPE_ALIASES[ext] = [ext]

SUPPORTED_UPLOAD_EXTS = set(DEFAULT_ALLOWED_FILE_EXTS)


def normalize_allowed_file_exts(value):
    if isinstance(value, (list, tuple, set)):
        raw_parts = []
        for item in value:
            raw_parts.extend(str(item or "").replace(";", ",").replace("|", ",").split(","))
    else:
        text = str(value or "").strip()
        raw_parts = re.split(r"[,;\s|]+", text) if text else []

    result = []
    for raw in raw_parts:
        key = str(raw or "").strip().lower().lstrip(".")
        if not key:
            continue

        for ext in FILE_TYPE_ALIASES.get(key, [key]):
            ext = str(ext or "").strip().lower().lstrip(".")
            if ext in SUPPORTED_UPLOAD_EXTS and ext not in result:
                result.append(ext)

    return result


def get_assignment_allowed_file_exts(assignment):
    raw_allowed = (
        assignment.get("allowed_file_types", "")
        or assignment.get("allowed_file_exts", "")
    )
    allowed = normalize_allowed_file_exts(
        raw_allowed
    )
    if not allowed and str(assignment.get("allow_link_submission", "")).strip():
        return []
    return allowed or DEFAULT_ALLOWED_FILE_EXTS[:]


def allowed_file_exts_text(exts):
    exts = normalize_allowed_file_exts(exts)
    if not exts:
        return "ไม่รับไฟล์แนบ"
    return ", ".join(f".{ext}" for ext in exts)


def add_assignment_file_type_metadata(assignment):
    add_assignment_due_metadata(assignment)
    allowed_exts = get_assignment_allowed_file_exts(assignment)
    assignment["allowed_file_types"] = ",".join(allowed_exts)
    assignment["allowed_file_exts"] = allowed_exts
    assignment["allowed_file_types_text"] = allowed_file_exts_text(allowed_exts)
    assignment["allowed_file_accept"] = ",".join(f".{ext}" for ext in allowed_exts)
    assignment["allow_link_submission"] = assignment_allows_link(assignment)
    return assignment


def file_ext_from_name(file_name):
    file_name = str(file_name or "").strip()
    if "." not in file_name:
        return ""
    return file_name.rsplit(".", 1)[-1].lower()


def replace_file_extension(file_name, new_ext):
    file_name = clean_drive_name(file_name, "upload_file")
    base = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    return f"{base}.{str(new_ext or '').lstrip('.')}"


def convert_heic_to_jpeg(file_bytes, file_name):
    ext = file_ext_from_name(file_name)
    if ext not in {"heic", "heif"}:
        return file_bytes, file_name

    try:
        from PIL import Image
    except Exception as e:
        raise ValueError("ระบบยังไม่พร้อมแปลงไฟล์ HEIC กรุณาส่งเป็น JPG/PNG หรืออัปโหลดเป็นลิงก์") from e

    image = None
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        with Image.open(BytesIO(file_bytes)) as image_obj:
            image = image_obj.copy()
    except Exception:
        try:
            import pyheif
            heif_file = pyheif.read(file_bytes)
            image = Image.frombytes(
                heif_file.mode,
                heif_file.size,
                heif_file.data,
                "raw",
                heif_file.mode,
                heif_file.stride,
            )
        except Exception as e:
            raise ValueError(
                "ระบบยังไม่พร้อมแปลงไฟล์ HEIC กรุณาส่งเป็น JPG/PNG หรืออัปโหลดเป็นลิงก์"
            ) from e

    if image is None:
        raise ValueError("ไม่สามารถแปลงไฟล์ HEIC ได้ในขณะนี้")

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue(), replace_file_extension(file_name, "jpg")


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    text = str(value).strip().lower()
    if not text:
        return default

    if text in {"1", "true", "yes", "y", "on", "allow", "allowed", "ใช่", "อนุญาต"}:
        return True
    if text in {"0", "false", "no", "n", "off", "deny", "denied", "ไม่", "ไม่อนุญาต"}:
        return False

    return default


def assignment_allows_link(assignment):
    if not assignment:
        return True

    value = assignment.get("allow_link_submission", "")
    if str(value or "").strip() == "":
        return True

    return parse_bool(value, default=True)


def setup_base_sheets(sh=None):
    sh = sh or get_spreadsheet()
    result = {
        "spreadsheet_id": getattr(sh, "id", ""),
        "spreadsheet_url": getattr(sh, "url", ""),
        "created": [],
        "updated": [],
        "unchanged": [],
    }

    existing_titles = [ws.title for ws in sh.worksheets()]

    for sheet_name, headers in BASE_SHEETS.items():
        if sheet_name in existing_titles:
            ws = sh.worksheet(sheet_name)
            created_sheet = False
        else:
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=max(20, len(headers) + 5))
            created_sheet = True

        current_headers = call_google_sheet_api(lambda: ws.row_values(1))
        if not current_headers:
            call_google_sheet_api(lambda: ws.append_row(headers))
            invalidate_sheet_cache(sheet_name)
            result["created" if created_sheet else "updated"].append(sheet_name)
        else:
            # เพิ่ม header ที่ยังไม่มี
            changed = False
            for h in headers:
                if h not in current_headers:
                    current_headers.append(h)
                    changed = True
            if changed:
                call_google_sheet_api(lambda: ws.update("1:1", [current_headers]))
                invalidate_sheet_cache(sheet_name)
                result["updated"].append(sheet_name)
            elif created_sheet:
                result["created"].append(sheet_name)
            else:
                result["unchanged"].append(sheet_name)

    return result


def create_fresh_base_spreadsheet(title=None):
    title = str(title or "").strip() or f"line-school-bot-{now_dt().strftime('%Y%m%d-%H%M%S')}"
    gc = get_gspread_client()
    sh = gc.create(title)

    # ลบชีตเปล่าเริ่มต้นในไฟล์ใหม่ เพื่อให้เหลือเฉพาะ tab ของระบบ
    for ws in sh.worksheets():
        if ws.title not in BASE_SHEETS:
            try:
                sh.del_worksheet(ws)
            except Exception:
                pass

    result = setup_base_sheets(sh)
    result["title"] = title
    return result


def prune_current_spreadsheet(keep_classroom_sheets=True):
    sh = get_spreadsheet()
    result = setup_base_sheets(sh)
    kept_titles = set(BASE_SHEETS.keys())

    deleted = []
    kept_extra = []
    for ws in sh.worksheets():
        if ws.title in kept_titles:
            continue
        if keep_classroom_sheets and (
            ws.title.startswith("ห้อง_") or ws.title.startswith("เช็คชื่อ_")
        ):
            kept_extra.append(ws.title)
            continue

        sh.del_worksheet(ws)
        deleted.append(ws.title)

    result["deleted"] = deleted
    result["kept_extra"] = kept_extra
    return result


def ensure_headers(ws, headers):
    sheet_name = ws.title
    current = get_sheet_headers(sheet_name)
    if not current:
        call_google_sheet_api(lambda: ws.append_row(headers))
        invalidate_sheet_cache(sheet_name)
        return headers

    changed = False
    for h in headers:
        if h not in current:
            current.append(h)
            changed = True

    if changed:
        call_google_sheet_api(lambda: ws.update("1:1", [current]))
        invalidate_sheet_cache(sheet_name)

    return current


def update_cell_raw(ws, row, col, value):
    cell = gspread.utils.rowcol_to_a1(row, col)
    return ws.update([[str(value)]], range_name=cell, raw=True)


def update_named_cell_raw(ws, headers, row, header, value):
    if header in headers:
        update_cell_raw(ws, row, headers.index(header) + 1, value)


# =========================================================
# Helpers: Records
# =========================================================

def find_record_by_value(sheet_name, key, value):
    records = get_sheet_records(sheet_name)

    for i, r in enumerate(records, start=2):
        if str(r.get(key, "")).strip() == str(value).strip():
            return i, r

    return None, None


def first_record_value(record, keys):
    for key in keys:
        value = str(record.get(key, "")).strip()
        if value:
            return value
    return ""


def split_line_user_ids(value):
    text = str(value or "").strip()
    if not text:
        return []
    return [
        item.strip()
        for item in re.split(r"[,;\s|]+", text)
        if item.strip()
    ]


def unique_line_user_ids(*values):
    result = []
    for value in values:
        items = value if isinstance(value, (list, tuple, set)) else split_line_user_ids(value)
        for item in items:
            item = str(item or "").strip()
            if item and item not in result:
                result.append(item)
    return result


def student_line_user_ids_for_record(record):
    if not record:
        return []
    return unique_line_user_ids(
        record.get("student_line_user_id", ""),
        record.get("line_user_ids", ""),
    )


def student_line_user_ids_for_records(records):
    result = []
    for record in records or []:
        result = unique_line_user_ids(result, student_line_user_ids_for_record(record))
    return result


def record_has_line_user_id(record, user_id):
    user_id = str(user_id or "").strip()
    return bool(user_id and user_id in student_line_user_ids_for_record(record))


def row_values_for_headers(headers, values_by_header):
    return [values_by_header.get(h, "") for h in headers]


def parse_float_value(value, default=0.0):
    try:
        text = str(value or "").strip()
        if text == "":
            return default
        return float(text)
    except Exception:
        return default


def format_score_value(value):
    try:
        value = float(value)
    except Exception:
        return ""
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def student_code_sort_key(student):
    code = str(student.get("student_code", "")).strip()
    name = str(student.get("student_name", "")).strip()

    match = re.search(r"\d+", code)
    if match:
        return (0, int(match.group(0)), code, name)

    return (1, code, name)


def sort_students_by_code(students):
    return sorted(students or [], key=student_code_sort_key)


DEFAULT_DUE_TIME = "23:59"


def normalize_time_text(value, default=""):
    text = str(value or "").strip()
    if not text:
        return default

    text = text.replace("น.", "").replace("น", "").strip()
    text = text.replace(".", ":")

    compact_match = re.fullmatch(r"(\d{1,2})(\d{2})", text)
    if compact_match:
        text = f"{compact_match.group(1)}:{compact_match.group(2)}"

    match = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::\d{1,2})?", text)
    if not match:
        return default

    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return default

    return f"{hour:02d}:{minute:02d}"


def assignment_due_time(assignment):
    return normalize_time_text(
        assignment.get("due_time", "") if assignment else "",
        DEFAULT_DUE_TIME,
    )


def assignment_due_text(assignment):
    if not assignment:
        return ""

    due_date = str(assignment.get("due_date", "")).strip()
    due_time = assignment_due_time(assignment)

    if due_date and due_time:
        return f"{due_date} เวลา {due_time} น."
    if due_date:
        return due_date
    if due_time:
        return f"เวลา {due_time} น."
    return ""


def add_assignment_due_metadata(assignment):
    assignment["due_time"] = assignment_due_time(assignment)
    assignment["due_text"] = assignment_due_text(assignment)
    return assignment


def get_student_by_line_user_id(user_id):
    db_student = get_registration_from_db(user_id)
    if db_student:
        return db_student
    user_id = str(user_id or "").strip()
    for _, r in enumerate(get_sheet_records("students"), start=2):
        if record_has_line_user_id(r, user_id):
            return r
    return None


def teacher_rooms_text_from_record(record):
    return first_record_value(record, [
        "rooms",
        "room",
        "classroom",
        "classrooms",
    ])


def get_teacher_records_by_line_user_id(user_id):
    user_id = str(user_id or "").strip()
    if not user_id:
        return []

    records = get_sheet_records("teachers")
    matched = []

    for r in records:
        row_user_id = first_record_value(r, [
            "teacher_line_user_id",
            "line_user_id",
            "user_id",
        ])
        if row_user_id == user_id:
            matched.append(r)

    return matched


def get_teacher_by_line_user_id(user_id):
    matched = get_teacher_records_by_line_user_id(user_id)
    if not matched:
        return None

    for r in matched:
        if teacher_rooms_text_from_record(r):
            return r

    return matched[0]


def get_teacher_rooms(user_id):
    teacher_records = get_teacher_records_by_line_user_id(user_id)
    if not teacher_records:
        return []

    rooms = []
    for teacher in teacher_records:
        rooms_text = teacher_rooms_text_from_record(teacher)
        for room in normalize_rooms_text(rooms_text):
            if room not in rooms:
                rooms.append(room)

    return rooms


def validate_teacher_classroom_access(user_id, classroom, empty_message):
    teacher = get_teacher_by_line_user_id(user_id)
    if not teacher:
        return None, "คำสั่งนี้ใช้ได้เฉพาะครู"

    classroom = normalize_classroom_text(classroom)
    if not classroom:
        return teacher, empty_message

    rooms = get_teacher_rooms(user_id)
    if not rooms:
        return teacher, "ยังไม่ได้ตั้งค่าห้องที่ดูแล"

    if classroom not in rooms:
        return teacher, "คุณไม่ได้ดูแลห้องนี้"

    return teacher, ""


def get_teacher_managed_rooms_or_error(user_id):
    teacher = get_teacher_by_line_user_id(user_id)
    if not teacher:
        return None, [], "คำสั่งนี้ใช้ได้เฉพาะครู"

    rooms = get_teacher_rooms(user_id)
    if not rooms:
        return teacher, [], "ยังไม่ได้ตั้งค่าห้องที่ดูแล"

    return teacher, rooms, ""


def update_all_classroom_sheets_for_teacher(user_id, rooms=None, only_dirty=True):
    if rooms is None:
        _, rooms, access_error = get_teacher_managed_rooms_or_error(user_id)
    else:
        rooms = normalize_rooms_text(",".join(rooms))
        access_error = "" if rooms else "ยังไม่ได้ตั้งค่าห้องที่ดูแล"

    if access_error:
        return {
            "success": False,
            "message": access_error,
            "rooms": [],
            "updated": [],
            "failed": [],
        }

    requested_rooms = rooms[:]
    skipped = []
    if only_dirty:
        dirty_rooms = set(get_dirty_rooms())
        rooms = [room for room in rooms if room in dirty_rooms]
        skipped = [room for room in requested_rooms if room not in dirty_rooms]

    updated = []
    failed = []

    for classroom in rooms:
        try:
            create_or_update_classroom_sheet(classroom)
            create_or_update_attendance_sheet(classroom)
            updated.append(classroom)
            invalidate_sheet_cache(classroom_sheet_name(classroom))
            invalidate_sheet_cache(attendance_sheet_name(classroom))
        except Exception as e:
            failed.append({
                "classroom": classroom,
                "message": str(e),
            })

    clear_dirty_rooms(updated)

    return {
        "success": not failed,
        "message": "อัปเดตชีตเสร็จแล้ว" if rooms else "ไม่มีห้องที่ต้องอัปเดต",
        "rooms": requested_rooms,
        "target_rooms": rooms,
        "skipped": skipped,
        "updated": updated,
        "failed": failed,
    }


def sync_all_scores_for_teacher(user_id, rooms=None):
    if rooms is None:
        _, rooms, access_error = get_teacher_managed_rooms_or_error(user_id)
    else:
        rooms = normalize_rooms_text(",".join(rooms))
        access_error = "" if rooms else "ยังไม่ได้ตั้งค่าห้องที่ดูแล"

    if access_error:
        return {
            "success": False,
            "message": access_error,
            "rooms": [],
            "results": [],
            "failed": [],
            "updated": 0,
        }

    results = []
    failed = []
    total_updated = 0

    for classroom in rooms:
        try:
            result = sync_scores_from_classroom_sheet(classroom)
            updated = int(result.get("updated", 0) or 0)
            if result.get("success"):
                total_updated += updated
                results.append({
                    "classroom": classroom,
                    "message": result.get("message", ""),
                    "updated": updated,
                    "success": True,
                })
            else:
                failed.append({
                    "classroom": classroom,
                    "message": result.get("message", "ซิงก์คะแนนไม่สำเร็จ"),
                })
        except Exception as e:
            failed.append({
                "classroom": classroom,
                "message": str(e),
            })

    return {
        "success": not failed,
        "message": "ซิงก์คะแนนเสร็จแล้ว",
        "rooms": rooms,
        "results": results,
        "failed": failed,
        "updated": total_updated,
    }


def parse_room_scoped_command(text, command_prefixes):
    raw_text = str(text or "").strip()
    if not raw_text:
        return False, ""

    for prefix in command_prefixes:
        prefix_text = str(prefix or "").strip()
        if not prefix_text:
            continue

        if raw_text.upper() == prefix_text.upper():
            return True, ""

        if raw_text.upper().startswith(prefix_text.upper()):
            room_text = raw_text[len(prefix_text):].strip()
            room_text = room_text.lstrip(":：- ").strip()
            return True, normalize_classroom_text(room_text)

    return False, ""


def update_classroom_sheet_for_teacher(user_id, classroom):
    classroom = normalize_classroom_text(classroom)
    _, access_error = validate_teacher_classroom_access(
        user_id,
        classroom,
        "กรุณาพิมพ์ เช่น:\nUU 401",
    )
    if access_error:
        return {
            "success": False,
            "message": access_error,
            "classroom": classroom,
        }

    create_or_update_classroom_sheet(classroom)
    create_or_update_attendance_sheet(classroom)
    clear_dirty_rooms([classroom])
    return {
        "success": True,
        "message": f"อัปเดตชีตห้อง {classroom} เรียบร้อยแล้ว",
        "classroom": classroom,
    }


def sync_classroom_scores_for_teacher(user_id, classroom):
    classroom = normalize_classroom_text(classroom)
    _, access_error = validate_teacher_classroom_access(
        user_id,
        classroom,
        "กรุณาพิมพ์ เช่น:\nSS 401",
    )
    if access_error:
        return {
            "success": False,
            "message": access_error,
            "classroom": classroom,
            "updated": 0,
        }

    result = sync_scores_from_classroom_sheet(classroom)
    result["classroom"] = classroom
    return result


def get_class_group_id(classroom):
    records = get_sheet_records("class_groups")

    for r in records:
        if str(r.get("classroom", "")).strip() == str(classroom).strip():
            return str(r.get("group_id", "")).strip()

    return ""


def upsert_class_group(classroom, group_id):
    ws = get_worksheet("class_groups")
    ensure_headers(ws, BASE_SHEETS["class_groups"])

    records = get_sheet_records("class_groups")
    headers = get_sheet_headers("class_groups")

    for i, r in enumerate(records, start=2):
        if str(r.get("classroom", "")).strip() == str(classroom).strip():
            ws.batch_update([
                {
                    "range": f"{col_letter(headers.index('group_id') + 1)}{i}",
                    "values": [[group_id]],
                },
                {
                    "range": f"{col_letter(headers.index('updated_at') + 1)}{i}",
                    "values": [[now_text()]],
                },
            ], value_input_option="USER_ENTERED")
            invalidate_sheet_cache("class_groups")
            return

    buffered_append_row(ws, [
        classroom,
        group_id,
        now_text(),
    ])
    invalidate_sheet_cache("class_groups")


def mark_room_dirty(classroom, reason=""):
    """
    บันทึกว่าห้องนี้มีข้อมูลเปลี่ยนแล้ว แต่ยังไม่ rebuild ชีตสรุปทันที
    เพื่อลด Google Sheets API quota
    """
    classroom = str(classroom or "").strip()
    if not classroom:
        return

    try:
        dirty_lock = get_operation_lock(("dirty_room", classroom))
        with dirty_lock:
            invalidate_sheet_cache("dirty_rooms")
            ws = get_worksheet("dirty_rooms")
            ensure_headers(ws, BASE_SHEETS["dirty_rooms"])
            records = get_sheet_records("dirty_rooms")
            headers = get_sheet_headers("dirty_rooms")

            for i, r in enumerate(records, start=2):
                if str(r.get("classroom", "")).strip() == classroom:
                    call_google_sheet_api(
                        lambda: ws.batch_update([
                            {
                                "range": f"{col_letter(headers.index('reason') + 1)}{i}",
                                "values": [[reason]],
                            },
                            {
                                "range": f"{col_letter(headers.index('updated_at') + 1)}{i}",
                                "values": [[now_text()]],
                            },
                        ], value_input_option="USER_ENTERED")
                    )
                    invalidate_sheet_cache("dirty_rooms")
                    return

            buffered_append_row(ws, [classroom, reason, now_text()], value_input_option="USER_ENTERED")
            invalidate_sheet_cache("dirty_rooms")
    except Exception as e:
        print("[mark_room_dirty] Error:", e)


def get_dirty_rooms():
    try:
        ws = get_worksheet("dirty_rooms")
        ensure_headers(ws, BASE_SHEETS["dirty_rooms"])
        records = get_sheet_records("dirty_rooms")
        return [
            normalize_classroom_text(r.get("classroom", ""))
            for r in records
            if normalize_classroom_text(r.get("classroom", ""))
        ]
    except Exception as e:
        print("[get_dirty_rooms] Error:", e)
        return []


def clear_dirty_rooms(classrooms):
    classrooms = set(normalize_rooms_text(",".join(classrooms or [])))
    if not classrooms:
        return

    try:
        ws = get_worksheet("dirty_rooms")
        records = get_sheet_records("dirty_rooms")
        rows_to_delete = [
            row_i
            for row_i, r in enumerate(records, start=2)
            if normalize_classroom_text(r.get("classroom", "")) in classrooms
        ]
        for row_i in reversed(rows_to_delete):
            ws.delete_rows(row_i)
        invalidate_sheet_cache("dirty_rooms")
    except Exception as e:
        print("[clear_dirty_rooms] Error:", e)


# =========================================================
# Classroom Sheet / Classroom Report
# =========================================================

try:
    from gspread_formatting import (
        CellFormat,
        Color,
        TextFormat,
        format_cell_range,
        set_column_width,
        set_frozen,
        batch_updater,
    )
except ModuleNotFoundError:
    print("[WARN] gspread_formatting not installed; classroom sheet formatting will be skipped.")

    def Color(*args, **kwargs):
        return None

    def TextFormat(*args, **kwargs):
        return None

    def CellFormat(*args, **kwargs):
        return None

    def format_cell_range(*args, **kwargs):
        return None

    def set_column_width(*args, **kwargs):
        return None

    def set_frozen(*args, **kwargs):
        return None

    class batch_updater:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False


def classroom_sheet_name(classroom):
    return f"ห้อง_{classroom}"


def attendance_sheet_name(classroom):
    return f"เช็คชื่อ_{classroom}"


def attendance_session_key(record):
    return (
        str(record.get("attendance_date", "")).strip(),
        normalize_time_text(record.get("start_time", ""), ""),
        normalize_time_text(record.get("end_time", ""), ""),
    )


def attendance_session_sort_key(session):
    date_text, start_time, end_time = session
    parsed_date = parse_date(date_text)
    date_ord = parsed_date.toordinal() if parsed_date else 0
    return (date_ord, start_time, end_time, date_text)


def attendance_session_header_label(session, index):
    date_text, start_time, end_time = session
    label_date = date_text
    parsed_date = parse_date(date_text)
    if parsed_date:
        label_date = parsed_date.strftime("%d/%m/%Y")
    return f"คาบ {index} ({label_date} {start_time}-{end_time})"


ATTENDANCE_SUMMARY_STATUSES = ["มา", "กิจกรรม", "สาย", "ลา", "ขาด", "หนี"]


def student_sort_key_for_sheet(student):
    try:
        return int(str(student.get("student_code", "")).strip())
    except Exception:
        return 9999


def create_or_update_attendance_sheet(classroom):
    """
    สร้าง/อัปเดตแท็บเช็คชื่อของห้อง:
    แนวตั้ง = ชื่อนักเรียน | แนวนอน = คาบเรียน | สรุปจำนวนสถานะด้านล่าง
    """
    classroom = normalize_classroom_text(classroom)
    sheet_name = attendance_sheet_name(classroom)
    ws = get_worksheet(sheet_name)

    students = get_students_by_classroom(classroom)
    students.sort(key=student_sort_key_for_sheet)

    records = [
        r for r in get_sheet_records("attendance")
        if normalize_classroom_text(r.get("classroom", "")) == classroom
    ]

    sessions = sorted(
        {
            attendance_session_key(r)
            for r in records
            if attendance_session_key(r)[0]
        },
        key=attendance_session_sort_key,
    )

    cell_data = {}
    for record in records:
        sid = str(record.get("student_line_user_id", "")).strip()
        session = attendance_session_key(record)
        if not sid or not session[0]:
            continue
        cell_data[(sid, session)] = {
            "status": str(record.get("status", "")).strip() or "มา",
            "note": str(record.get("note", "")).strip(),
        }

    header = ["ชื่อ", "เลขที่"]
    for index, session in enumerate(sessions, start=1):
        header.append(attendance_session_header_label(session, index))
    header.extend([f"รวม{s}" for s in ATTENDANCE_SUMMARY_STATUSES])
    header.append("หมายเหตุ")

    values = [header]
    student_count = len(students)

    for student in students:
        sid = str(student.get("student_line_user_id", "")).strip()
        row = [
            str(student.get("student_name", "")).strip(),
            str(student.get("student_code", "")).strip(),
        ]
        status_counts = {status: 0 for status in ATTENDANCE_SUMMARY_STATUSES}
        note_parts = []

        for index, session in enumerate(sessions, start=1):
            cell = cell_data.get((sid, session), {})
            status = cell.get("status", "")
            row.append(status)
            if status in status_counts:
                status_counts[status] += 1
            note = cell.get("note", "")
            if note:
                note_parts.append(f"{attendance_session_header_label(session, index)}: {note}")

        row.extend(status_counts[status] for status in ATTENDANCE_SUMMARY_STATUSES)
        row.append("\n".join(note_parts))
        values.append(row)

    if student_count:
        values.append([""] * len(header))

        for summary_status in ATTENDANCE_SUMMARY_STATUSES:
            summary_row = [f"สรุป {summary_status}", ""]
            for session in sessions:
                count = sum(
                    1
                    for student in students
                    if cell_data.get(
                        (str(student.get("student_line_user_id", "")).strip(), session),
                        {},
                    ).get("status") == summary_status
                )
                summary_row.append(count)
            summary_row.extend([""] * len(ATTENDANCE_SUMMARY_STATUSES))
            summary_row.append("")
            values.append(summary_row)

    ws.clear()
    if values:
        ws.update("A1", values)

    total_cols = len(header)
    last_data_row = len(values)

    try:
        header_format = CellFormat(
            backgroundColor=Color(0.90, 0.90, 0.90),
            textFormat=TextFormat(bold=True),
            horizontalAlignment="CENTER",
            verticalAlignment="MIDDLE",
        )
        center_format = CellFormat(
            horizontalAlignment="CENTER",
            verticalAlignment="MIDDLE",
        )
        summary_format = CellFormat(
            backgroundColor=Color(0.93, 0.96, 1.0),
            textFormat=TextFormat(bold=True),
            horizontalAlignment="CENTER",
            verticalAlignment="MIDDLE",
        )
        note_format = CellFormat(
            horizontalAlignment="LEFT",
            verticalAlignment="TOP",
        )

        with batch_updater(get_spreadsheet()) as batch:
            end_col_letter = col_letter(total_cols)
            format_cell_range(ws, f"A1:{end_col_letter}1", header_format)
            if last_data_row > 1:
                format_cell_range(ws, f"A2:{end_col_letter}{last_data_row}", center_format)
                format_cell_range(ws, f"{col_letter(total_cols)}2:{col_letter(total_cols)}{student_count + 1}", note_format)

            if student_count:
                summary_start = student_count + 3
                format_cell_range(
                    ws,
                    f"A{summary_start}:{end_col_letter}{last_data_row}",
                    summary_format,
                )

        set_frozen(ws, rows=1, cols=2)
        set_column_width(ws, "A", 180)
        set_column_width(ws, "B", 70)
        for col_index in range(3, total_cols):
            set_column_width(ws, col_letter(col_index), 120)
        set_column_width(ws, col_letter(total_cols), 240)
    except Exception as e:
        print("[format attendance sheet] Error:", e)

    return ws


def col_letter(col):
    return gspread.utils.rowcol_to_a1(1, col).replace("1", "")


def google_sheet_hyperlink_formula(url, label="เปิดไฟล์"):
    url = str(url or "").strip()
    if not url:
        return ""

    safe_url = url.replace('"', '""')
    safe_label = str(label or "เปิดไฟล์").replace('"', '""')
    return f'=HYPERLINK("{safe_url}", "{safe_label}")'


def create_or_update_classroom_sheet(classroom):
    """
    สร้าง/อัปเดตหน้าต่างรวมของห้องให้เหมือนตัวอย่าง:
    ชื่อ | เลขที่ | ID Line | คะแนนรวม | งาน 1: ตรงเวลา/เลยกำหนด/คะแนน/คอมเมนต์/ลิงก์ไฟล์ | งาน 2: ...
    """
    sheet_name = classroom_sheet_name(classroom)
    ws = get_worksheet(sheet_name)

    students = get_students_by_classroom(classroom)
    assignments = get_assignments_by_classroom(classroom)

    # เรียงนักเรียนตามเลขที่
    def student_sort_key(s):
        try:
            return int(str(s.get("student_code", "")).strip())
        except Exception:
            return 9999

    students.sort(key=student_sort_key)

    # เรียงงานตามวันที่สร้าง
    def assignment_sort_key(a):
        return str(a.get("created_at", "")) + str(a.get("assignment_id", ""))

    assignments.sort(key=assignment_sort_key)

    assignment_ids = {
        str(a.get("assignment_id", "")).strip()
        for a in assignments
        if str(a.get("assignment_id", "")).strip()
    }
    student_line_user_ids = {
        str(s.get("student_line_user_id", "")).strip()
        for s in students
        if str(s.get("student_line_user_id", "")).strip()
    }
    submission_index = get_submissions_index(
        classroom=classroom,
        assignment_ids=assignment_ids,
        student_line_user_ids=student_line_user_ids,
    )
    exam_scores = get_exam_scores_by_classroom(classroom)

    # ล้างแล้วสร้างใหม่ทั้งชีต เพื่อให้หัวตาราง/merge/สีตรงเสมอ
    ws.clear()

    base_col_count = 10
    row1 = ["", "", "", "คะแนนรวม", "", "", "", "", "", ""]
    row2 = ["", "", "", "", "", "", "", "", "", ""]
    row3 = [
        "ชื่อ",
        "เลขที่",
        "ID Line",
        "กลางภาค 20",
        "ปลายภาค 20",
        "งาน/สมุด/ท้ายบท 60",
        "งานที่สั่ง",
        "แบบทดสอบท้ายบท",
        "คะแนนสมุด",
        "รวม 100",
    ]

    for i, assignment in enumerate(assignments, start=1):
        title = str(assignment.get("title", "")).strip() or f"งาน {i}"
        title = title + assignment_weight_label(assignment)
        row1.extend(["งาน" if i == 1 else "", "", "", "", "", ""])
        row2.extend([title, "", "", "", "", ""])
        row3.extend(["ตรวจเวลา", "เลยกำหนด", "คะแนนส่ง", "คะแนน", "คอมเมนต์", "ลิงก์ไฟล์"])

    values = [row1, row2, row3]

    hyperlink_updates = []
    assignment_col_count = 6

    for student in students:
        student_line_user_id = str(student.get("student_line_user_id", "")).strip()
        student_name = str(student.get("student_name", "")).strip()
        student_code = str(student.get("student_code", "")).strip()
        sheet_row = len(values) + 1

        row = [
            student_name,
            student_code,
            student_line_user_id,
        ]
        totals = calculate_student_total_score(
            student_line_user_id,
            assignments,
            submission_index,
            exam_scores,
        )
        row.extend([
            format_score_value(totals["midterm"]),
            format_score_value(totals["final"]),
            format_score_value(totals["coursework"]),
            format_score_value(totals["assignment"]),
            format_score_value(totals["quiz"]),
            format_score_value(totals["notebook"]),
            format_score_value(totals["total"]),
        ])

        for idx, assignment in enumerate(assignments):
            assignment_id = str(assignment.get("assignment_id", "")).strip()
            sub = submission_index.get((student_line_user_id, assignment_id))

            if sub:
                submitted_at = str(sub.get("submitted_at", "")).strip()
                late = str(sub.get("late", "")).strip()
                auto_score, score = submission_score_values_for_sheet(sub, assignment)
                teacher_comment = submission_comment_value_for_sheet(sub, auto_score, score)
                file_url = str(sub.get("file_url", "")).strip()
                file_label = "เปิดไฟล์" if file_url else ""
                row.extend([submitted_at, late, auto_score, score, teacher_comment, file_label])

                formula = google_sheet_hyperlink_formula(file_url)
                if formula:
                    link_col = base_col_count + idx * assignment_col_count + 6
                    hyperlink_updates.append({
                        "range": f"{col_letter(link_col)}{sheet_row}",
                        "values": [[formula]],
                    })
            else:
                row.extend(["", "", "", "", "", ""])

        values.append(row)

    ws.update("A1", values)

    if hyperlink_updates:
        ws.batch_update(hyperlink_updates, value_input_option="USER_ENTERED")

    total_cols = base_col_count + len(assignments) * assignment_col_count

    # Merge หัวตาราง
    try:
        ws.merge_cells("A1:C2")
        ws.merge_cells("D1:J2")
    except Exception:
        pass

    if total_cols >= base_col_count + 1:
        try:
            ws.merge_cells(1, base_col_count + 1, 1, total_cols)
        except Exception:
            pass

    col = base_col_count + 1
    for assignment in assignments:
        try:
            ws.merge_cells(2, col, 2, col + assignment_col_count - 1)
        except Exception:
            pass
        col += assignment_col_count

    # Format
    header_format = CellFormat(
        backgroundColor=Color(0.90, 0.90, 0.90),
        textFormat=TextFormat(bold=True),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
    )

    left_header_format = CellFormat(
        backgroundColor=Color(0.95, 0.95, 0.95),
        textFormat=TextFormat(bold=True),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
    )

    assignment_odd = CellFormat(
        backgroundColor=Color(0.96, 0.86, 0.78),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
    )

    assignment_even = CellFormat(
        backgroundColor=Color(0.78, 0.88, 0.72),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
    )

    center_format = CellFormat(
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
    )

    try:
        with batch_updater(get_spreadsheet()) as batch:
            format_cell_range(ws, "A1:C3", left_header_format)

            if total_cols >= base_col_count + 1:
                end_col_letter = col_letter(total_cols)
                format_cell_range(ws, f"D1:{end_col_letter}3", header_format)
                format_cell_range(ws, f"A4:{end_col_letter}1000", center_format)

            col = base_col_count + 1
            for idx, assignment in enumerate(assignments):
                start_letter = col_letter(col)
                end_letter = col_letter(col + assignment_col_count - 1)

                color_format = assignment_odd if idx % 2 == 0 else assignment_even
                format_cell_range(ws, f"{start_letter}2:{end_letter}1000", color_format)

                col += assignment_col_count

        set_frozen(ws, rows=3, cols=3)

        set_column_width(ws, "A", 180)
        set_column_width(ws, "B", 80)
        set_column_width(ws, "C", 220)
        for col_name in ["D", "E", "F", "G", "H", "I", "J"]:
            set_column_width(ws, col_name, 110)

        col = base_col_count + 1
        for assignment in assignments:
            set_column_width(ws, col_letter(col), 120)
            set_column_width(ws, col_letter(col + 1), 100)
            set_column_width(ws, col_letter(col + 2), 80)
            set_column_width(ws, col_letter(col + 3), 80)
            set_column_width(ws, col_letter(col + 4), 180)
            set_column_width(ws, col_letter(col + 5), 220)
            col += assignment_col_count

    except Exception as e:
        print("[format classroom sheet] Error:", e)

    return ws


def sync_scores_from_classroom_sheet(classroom):
    """
    ครูกรอกคะแนน/คอมเมนต์ในชีตสรุปห้อง แล้ว sync กลับเข้า submissions
    """
    classroom = str(classroom or "").strip()
    if not classroom:
        return {
            "success": False,
            "message": "ไม่พบเลขห้อง",
            "updated": 0,
        }

    summary_ws = get_worksheet(classroom_sheet_name(classroom))
    values = summary_ws.get_all_values()

    if len(values) < 4:
        return {
            "success": False,
            "message": "ชีตสรุปยังไม่มีข้อมูล หรือยังไม่ได้อัปเดตชีตห้อง",
            "updated": 0,
        }

    assignments = get_assignments_by_classroom(classroom)

    def assignment_sort_key(a):
        return str(a.get("created_at", "")) + str(a.get("assignment_id", ""))

    assignments.sort(key=assignment_sort_key)

    if not assignments:
        return {
            "success": False,
            "message": "ยังไม่มีงานของห้องนี้",
            "updated": 0,
        }

    sub_ws = get_worksheet("submissions")
    ensure_headers(sub_ws, BASE_SHEETS["submissions"])
    sub_records = get_sheet_records("submissions")
    sub_headers = get_sheet_headers("submissions")

    missing_headers = [
        h for h in ["score", "teacher_comment", "checked_at"]
        if h not in sub_headers
    ]
    if missing_headers:
        return {
            "success": False,
            "message": "ไม่พบคอลัมน์ในชีต submissions: " + ", ".join(missing_headers),
            "updated": 0,
        }

    score_col_index = sub_headers.index("score") + 1
    comment_col_index = sub_headers.index("teacher_comment") + 1
    checked_at_col_index = sub_headers.index("checked_at") + 1

    sub_index = {}
    for row_i, r in enumerate(sub_records, start=2):
        sid = str(r.get("student_line_user_id", "")).strip()
        aid = str(r.get("assignment_id", "")).strip()
        if sid and aid:
            sub_index[(sid, aid)] = row_i

    updates = []
    skipped = 0
    updated_count = 0
    missing_scores = []
    summary_header_row = values[2] if len(values) > 2 else []
    try:
        assignment_start_idx = next(
            i for i, value in enumerate(summary_header_row)
            if str(value).strip() == "ตรวจเวลา"
        )
    except StopIteration:
        assignment_start_idx = 3

    summary_has_auto_score = (
        len(summary_header_row) > assignment_start_idx + 2
        and str(summary_header_row[assignment_start_idx + 2]).strip() == "คะแนนส่ง"
    )
    assignment_col_count = 6 if summary_has_auto_score else 5
    score_offset = 3 if summary_has_auto_score else 2
    comment_offset = 4 if summary_has_auto_score else 3

    for sheet_row, row in enumerate(values[3:], start=4):
        if len(row) < 3:
            continue

        student_line_user_id = str(row[2]).strip()
        if not student_line_user_id:
            continue

        for idx, assignment in enumerate(assignments):
            assignment_id = str(assignment.get("assignment_id", "")).strip()
            if not assignment_id:
                continue

            score_idx = assignment_start_idx + idx * assignment_col_count + score_offset
            comment_idx = assignment_start_idx + idx * assignment_col_count + comment_offset
            score = str(row[score_idx]).strip() if score_idx < len(row) else ""
            teacher_comment = str(row[comment_idx]).strip() if comment_idx < len(row) else ""
            if score == "" and teacher_comment == "":
                continue

            if score == "":
                student_name = str(row[0]).strip() if len(row) > 0 else ""
                student_code = str(row[1]).strip() if len(row) > 1 else ""
                assignment_title = str(assignment.get("title", "")).strip() or assignment_id
                student_label = student_name or student_line_user_id
                if student_code:
                    student_label = f"เลขที่ {student_code} {student_label}"
                missing_scores.append(
                    f"แถว {sheet_row}: {student_label} / {assignment_title}"
                )
                continue

            sub_row = sub_index.get((student_line_user_id, assignment_id))
            if not sub_row:
                skipped += 1
                continue

            updates.append({
                "range": f"{col_letter(score_col_index)}{sub_row}",
                "values": [[score]],
            })

            updates.append({
                "range": f"{col_letter(comment_col_index)}{sub_row}",
                "values": [[teacher_comment]],
            })

            updates.append({
                "range": f"{col_letter(checked_at_col_index)}{sub_row}",
                "values": [[now_text()]],
            })
            updated_count += 1

    if missing_scores:
        examples = "\n".join(f"- {item}" for item in missing_scores[:5])
        if len(missing_scores) > 5:
            examples += f"\n...อีก {len(missing_scores) - 5} รายการ"
        return {
            "success": False,
            "message": (
                "ยังไม่ซิงก์คะแนน เพราะพบรายการที่กรอกคอมเมนต์แต่ยังไม่กรอกคะแนน\n"
                + examples
                + "\n\nกรุณากรอกคะแนนก่อน แล้วค่อยสั่งซิงก์อีกครั้ง"
            ),
            "updated": 0,
            "skipped": skipped,
            "missing_score_count": len(missing_scores),
            "missing_scores": missing_scores[:20],
        }

    if updates:
        sub_ws.batch_update(updates, value_input_option="USER_ENTERED")
        invalidate_sheet_cache("submissions")

    return {
        "success": True,
        "message": f"ซิงก์คะแนนและคอมเมนต์ห้อง {classroom} เรียบร้อยแล้ว",
        "updated": updated_count,
        "skipped": skipped,
    }


def add_student_to_classroom_sheet(student_code, student_name, student_line_user_id, classroom):
    """
    นักเรียนลงทะเบียนแล้วไม่ rebuild ชีตทันที เพื่อลด quota ตอนลงพร้อมกัน
    """
    mark_room_dirty(classroom, "student_registered")


def add_assignment_header_to_classroom_sheet(classroom, assignment_title):
    """
    ครูสั่งงานใหม่แล้วไม่ rebuild ชีตทันที
    """
    mark_room_dirty(classroom, "assignment_created")


def mark_submission_in_classroom_sheet(classroom, student_line_user_id, assignment_title, file_url):
    """
    นักเรียนส่งงานแล้วไม่ rebuild ชีตทันที
    """
    mark_room_dirty(classroom, "submission_received")
    return True


# =========================================================
# Rich Menu Logic
# =========================================================

def student_has_pending_work(student_line_user_id):
    student = get_student_by_line_user_id(student_line_user_id)
    if not student:
        return False

    classroom = str(student.get("classroom", "")).strip()
    assignments = get_assignments_by_classroom(classroom)

    if not assignments:
        return False

    submissions = get_submissions_by_student(student_line_user_id)
    submitted_ids = set(str(s.get("assignment_id", "")).strip() for s in submissions)

    for a in assignments:
        aid = str(a.get("assignment_id", "")).strip()
        if aid and aid not in submitted_ids and assignment_requires_submission(a):
            return True

    return False


def student_has_unseen_answer(student_line_user_id):
    records = get_sheet_records("questions")

    for r in records:
        if (
            str(r.get("student_line_user_id", "")).strip() == student_line_user_id
            and str(r.get("status", "")).strip() == "answered"
            and str(r.get("student_seen", "")).strip() != "yes"
        ):
            return True

    return False


def update_student_rich_menu(student_line_user_id):
    if not student_line_user_id:
        return None

    student = get_student_by_line_user_id(student_line_user_id)

    if not student:
        if STUDENT_RICH_MENU_REGISTER_ID:
            return link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_REGISTER_ID)
        return None

    try:
        has_pending = student_has_pending_work(student_line_user_id)
    except Exception as e:
        print("[student richmenu pending check] Error:", e)
        has_pending = False

    try:
        has_answer = student_has_unseen_answer(student_line_user_id)
    except Exception as e:
        print("[student richmenu answer check] Error:", e)
        has_answer = False

    if has_pending and has_answer and STUDENT_RICH_MENU_BOTH_ALERT_ID:
        return link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_BOTH_ALERT_ID)
    elif has_pending and STUDENT_RICH_MENU_PENDING_ALERT_ID:
        return link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_PENDING_ALERT_ID)
    elif has_answer and STUDENT_RICH_MENU_ANSWER_ALERT_ID:
        return link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_ANSWER_ALERT_ID)
    elif STUDENT_RICH_MENU_NORMAL_ID:
        return link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_NORMAL_ID)
    return None


def link_student_default_rich_menu(student_line_user_id):
    if student_line_user_id and STUDENT_RICH_MENU_NORMAL_ID:
        return link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_NORMAL_ID)
    return None


def teacher_has_pending_questions(teacher_line_user_id):
    rooms = get_teacher_rooms(teacher_line_user_id)
    if not rooms:
        return False

    records = get_sheet_records("questions")

    for r in records:
        classroom = str(r.get("classroom", "")).strip()
        status = str(r.get("status", "")).strip()
        if classroom in rooms and status == "pending":
            return True

    return False


def update_teacher_rich_menu(teacher_line_user_id):
    if not teacher_line_user_id:
        return None

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)

    if not teacher:
        if TEACHER_RICH_MENU_SETUP_ID:
            return link_rich_menu_to_user(teacher_line_user_id, TEACHER_RICH_MENU_SETUP_ID)
        return None

    try:
        has_pending_questions = teacher_has_pending_questions(teacher_line_user_id)
    except Exception as e:
        print("[teacher richmenu question check] Error:", e)
        has_pending_questions = False

    if has_pending_questions and TEACHER_RICH_MENU_QUESTION_ALERT_ID:
        return link_rich_menu_to_user(teacher_line_user_id, TEACHER_RICH_MENU_QUESTION_ALERT_ID)
    elif TEACHER_RICH_MENU_NORMAL_ID:
        return link_rich_menu_to_user(teacher_line_user_id, TEACHER_RICH_MENU_NORMAL_ID)
    return None


# =========================================================
# Assignment / Submission Helpers
# =========================================================

def get_assignments_by_classroom(classroom):
    records = get_sheet_records("assignments")

    result = []
    for r in records:
        if str(r.get("classroom", "")).strip() == str(classroom).strip():
            item = add_assignment_due_metadata(dict(r))
            result.append(add_assignment_score_metadata(item))

    return result


def get_assignment_by_id(assignment_id):
    records = get_sheet_records("assignments")

    for r in records:
        if str(r.get("assignment_id", "")).strip() == str(assignment_id).strip():
            item = add_assignment_due_metadata(dict(r))
            return add_assignment_score_metadata(item)

    return None


def find_assignment_row(assignment_id):
    records = get_sheet_records("assignments")

    for row_i, r in enumerate(records, start=2):
        if str(r.get("assignment_id", "")).strip() == str(assignment_id).strip():
            return row_i, r

    return None, None


def get_submissions_by_student(student_line_user_id):
    student_line_user_id = str(student_line_user_id or "").strip()
    student = get_student_by_line_user_id(student_line_user_id)
    student_ids = student_line_user_ids_for_record(student) or [student_line_user_id]

    db_submissions = []
    for sid in student_ids:
        db_submissions.extend(get_submissions_from_db(sid))
    try:
        records = get_sheet_records("submissions")
        sheet_submissions = [
            r for r in records
            if str(r.get("student_line_user_id", "")).strip() in student_ids
        ]
    except Exception as e:
        print("[get_submissions_by_student sheets] Error:", e)
        sheet_submissions = []

    merged = {
        str(r.get("assignment_id", "")).strip(): r
        for r in sheet_submissions
        if str(r.get("assignment_id", "")).strip()
    }
    for r in db_submissions:
        assignment_id = str(r.get("assignment_id", "")).strip()
        if assignment_id:
            merged[assignment_id] = r
    return list(merged.values())


SCORE_CATEGORIES = {
    "assignment": "ใบงาน",
    "quiz": "สอบเก็บคะแนน",
    "notebook": "คะแนนสมุด",
}


def assignment_requires_submission(assignment):
    if not assignment:
        return True
    return normalize_score_category(assignment.get("score_category", "")) != "quiz"


def build_assignment_title(chapter_name, score_category, work_title=""):
    chapter = str(chapter_name or "").strip()
    work = str(work_title or "").strip()
    category = normalize_score_category(score_category)

    if category == "assignment" and work:
        suffix = work
    else:
        suffix = SCORE_CATEGORIES.get(category, "งาน")

    if chapter and suffix:
        return f"{chapter} - {suffix}"
    return chapter or suffix


def get_chapter_names_by_classroom(classroom):
    chapters = []
    seen = set()
    for assignment in get_assignments_by_classroom(classroom):
        chapter = str(assignment.get("chapter_name", "")).strip()
        if not chapter:
            title = str(assignment.get("title", "")).strip()
            if " - " in title:
                chapter = title.split(" - ", 1)[0].strip()
        if chapter and chapter not in seen:
            seen.add(chapter)
            chapters.append(chapter)
    return chapters


def normalize_score_category(value):
    text = str(value or "").strip().lower()
    thai_to_key = {
        "งาน": "assignment",
        "งานที่สั่ง": "assignment",
        "ใบงาน": "assignment",
        "แบบทดสอบ": "quiz",
        "แบบทดสอบท้ายบท": "quiz",
        "สอบเก็บคะแนน": "quiz",
        "quiz": "quiz",
        "สมุด": "notebook",
        "คะแนนสมุด": "notebook",
        "notebook": "notebook",
    }
    if text in SCORE_CATEGORIES:
        return text
    return thai_to_key.get(text, "assignment")


def parse_assignment_weight(value):
    text = str(value or "").strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except Exception:
        return None


def assignment_counts_in_coursework(assignment):
    weight = parse_assignment_weight(assignment.get("score_weight", ""))
    if weight is None:
        return False
    return weight > 0


def assignment_weight_label(assignment):
    weight = parse_assignment_weight(assignment.get("score_weight", ""))
    if weight is None:
        return ""
    if weight <= 0:
        return " (ไม่นับคะแนน)"
    return f" (น้ำหนัก {format_score_value(weight)})"


def assignment_show_score_to_students(assignment, classroom_visibility=None):
    if not assignment:
        return False
    text = str(assignment.get("show_score_to_students", "")).strip()
    if text:
        return parse_sheet_bool(text)
    if classroom_visibility and classroom_visibility.get("show_work_scores"):
        return True
    return False


def add_assignment_score_metadata(assignment):
    assignment["score_category"] = normalize_score_category(assignment.get("score_category", ""))
    assignment["score_category_label"] = SCORE_CATEGORIES.get(
        assignment["score_category"],
        SCORE_CATEGORIES["assignment"],
    )
    assignment["chapter_name"] = str(assignment.get("chapter_name", "")).strip()
    weight = parse_assignment_weight(assignment.get("score_weight", ""))
    assignment["score_weight"] = format_score_value(weight) if weight is not None else ""
    assignment["counts_in_coursework"] = assignment_counts_in_coursework(assignment)
    assignment["show_score_to_students"] = assignment_show_score_to_students(assignment)
    return assignment


def assignment_show_score_for_classroom(assignment, classroom):
    visibility = get_score_visibility_by_classroom(classroom)
    return assignment_show_score_to_students(assignment, visibility)


def student_assignment_payload(assignment):
    item = dict(assignment)
    item.pop("score_weight", None)
    return item


def get_exam_scores_by_classroom(classroom):
    records = get_sheet_records("exam_scores")
    result = {}
    classroom = str(classroom or "").strip()
    for r in records:
        if str(r.get("classroom", "")).strip() != classroom:
            continue
        sid = str(r.get("student_line_user_id", "")).strip()
        if sid:
            result[sid] = r
    return result


def score_from_submission(submission, assignment=None):
    if not submission:
        return None
    score_text = str(submission.get("score", "")).strip()
    if score_text == "":
        score_text = str(submission.get("auto_score", "")).strip()
    if score_text == "":
        return None
    return parse_float_value(score_text, None)


def submission_score_values_for_sheet(sub, assignment):
    if not sub:
        return "", ""
    auto_score = str(sub.get("auto_score", "")).strip()
    score = str(sub.get("score", "")).strip()
    checked_at = str(sub.get("checked_at", "")).strip()
    teacher_comment = str(sub.get("teacher_comment", "")).strip()
    if (
        score == auto_score
        or (score in {"ใช่", "ไม่ใช่", "yes", "no"} and teacher_comment == auto_score)
    ) and not checked_at:
        score = ""
    return (
        auto_score,
        score,
    )


def submission_comment_value_for_sheet(sub, auto_score, score):
    teacher_comment = str(sub.get("teacher_comment", "")).strip()
    checked_at = str(sub.get("checked_at", "")).strip()
    if teacher_comment == auto_score and not score and not checked_at:
        return ""
    return teacher_comment


def calculate_student_coursework_score(student_line_user_id, assignments, submission_index):
    total = 0.0
    category_totals = {
        "assignment": 0.0,
        "quiz": 0.0,
        "notebook": 0.0,
    }

    for assignment in assignments:
        assignment_id = str(assignment.get("assignment_id", "")).strip()
        if not assignment_id:
            continue

        weight = parse_assignment_weight(assignment.get("score_weight", ""))
        if weight is None or weight <= 0:
            continue

        max_score = parse_float_value(assignment.get("max_score", ""), 0)
        if max_score <= 0:
            max_score = weight

        sub = submission_index.get((student_line_user_id, assignment_id))
        raw_score = score_from_submission(sub, assignment)
        if raw_score is None:
            continue

        weighted = max(0.0, min(raw_score, max_score)) / max_score * weight
        category = normalize_score_category(assignment.get("score_category", ""))
        category_totals[category] = category_totals.get(category, 0.0) + weighted
        total += weighted

    total = min(total, 60.0)
    return total, category_totals


def calculate_student_total_score(student_line_user_id, assignments, submission_index, exam_scores):
    coursework, category_totals = calculate_student_coursework_score(
        student_line_user_id,
        assignments,
        submission_index,
    )
    exam = exam_scores.get(student_line_user_id, {})
    midterm = min(max(parse_float_value(exam.get("midterm_score", ""), 0), 0), 20)
    final = min(max(parse_float_value(exam.get("final_score", ""), 0), 0), 20)
    total = min(coursework + midterm + final, 100.0)
    return {
        "midterm": midterm,
        "final": final,
        "coursework": coursework,
        "assignment": category_totals.get("assignment", 0.0),
        "quiz": category_totals.get("quiz", 0.0),
        "notebook": category_totals.get("notebook", 0.0),
        "total": total,
    }



def parse_sheet_bool(value):
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "show", "เปิด", "ใช่"}


def bool_to_sheet_text(value):
    return "yes" if value else "no"


def get_score_visibility_by_classroom(classroom):
    classroom = normalize_classroom_text(classroom)
    try:
        records = get_sheet_records("score_visibility")
    except Exception:
        records = []

    for r in records:
        if normalize_classroom_text(r.get("classroom", "")) == classroom:
            legacy_exam = parse_sheet_bool(r.get("show_exam_scores", ""))
            return {
                "classroom": classroom,
                "show_midterm_scores": parse_sheet_bool(
                    r.get("show_midterm_scores", "")
                ) or legacy_exam,
                "show_final_scores": parse_sheet_bool(
                    r.get("show_final_scores", "")
                ) or legacy_exam,
                "show_work_scores": parse_sheet_bool(r.get("show_work_scores", "")),
                "show_exam_scores": legacy_exam,
            }

    return {
        "classroom": classroom,
        "show_midterm_scores": False,
        "show_final_scores": False,
        "show_work_scores": False,
        "show_exam_scores": False,
    }


def get_assignment_visibility_payload(classroom):
    assignments = get_assignments_by_classroom(classroom)
    visibility = get_score_visibility_by_classroom(classroom)

    def sort_key(item):
        return (
            str(item.get("chapter_name", "")).strip(),
            str(item.get("created_at", "")).strip(),
            str(item.get("assignment_id", "")).strip(),
        )

    assignments.sort(key=sort_key)
    return [
        {
            "assignment_id": str(a.get("assignment_id", "")).strip(),
            "title": str(a.get("title", "")).strip(),
            "chapter_name": str(a.get("chapter_name", "")).strip(),
            "score_category": a.get("score_category", ""),
            "score_category_label": a.get("score_category_label", ""),
            "show_score_to_students": assignment_show_score_to_students(a, visibility),
        }
        for a in assignments
        if str(a.get("assignment_id", "")).strip()
    ]


def attendance_summary_for_student(classroom, student_line_user_id):
    classroom = normalize_classroom_text(classroom)
    student_line_user_id = str(student_line_user_id or "").strip()
    try:
        records = get_sheet_records("attendance")
    except Exception:
        records = []

    status_counts = {
        "มา": 0,
        "กิจกรรม": 0,
        "สาย": 0,
        "ลา": 0,
        "ขาด": 0,
        "หนี": 0,
    }
    sessions = {}

    for r in records:
        if normalize_classroom_text(r.get("classroom", "")) != classroom:
            continue
        if str(r.get("student_line_user_id", "")).strip() != student_line_user_id:
            continue

        key = (
            str(r.get("attendance_date", "")).strip(),
            str(r.get("start_time", "")).strip(),
            str(r.get("end_time", "")).strip(),
        )
        status = str(r.get("status", "")).strip() or "มา"
        sessions[key] = status

    for status in sessions.values():
        if status not in status_counts:
            status_counts[status] = 0
        status_counts[status] += 1

    total = len(sessions)
    attended = status_counts.get("มา", 0) + status_counts.get("กิจกรรม", 0) + status_counts.get("สาย", 0)
    absent = status_counts.get("ขาด", 0) + status_counts.get("หนี", 0)
    excused = status_counts.get("ลา", 0)
    not_attended = absent + excused
    attendance_percent = (attended / total * 100) if total else 0
    not_attended_percent = (not_attended / total * 100) if total else 0
    absent_percent = (absent / total * 100) if total else 0

    return {
        "total_sessions": total,
        "attended_sessions": attended,
        "absent_sessions": absent,
        "excused_sessions": excused,
        "not_attended_sessions": not_attended,
        "attendance_percent": round(attendance_percent, 2),
        "not_attended_percent": round(not_attended_percent, 2),
        "absent_percent": round(absent_percent, 2),
        "no_exam": absent_percent > 20,
        "status_counts": status_counts,
    }


def student_score_summary_for_announcement(student):
    classroom = normalize_classroom_text(student.get("classroom", ""))
    student_line_user_id = str(student.get("student_line_user_id", "")).strip()
    visibility = get_score_visibility_by_classroom(classroom)

    visibility = get_score_visibility_by_classroom(classroom)

    assignments = get_assignments_by_classroom(classroom)
    visible_assignments = [
        a for a in assignments
        if assignment_show_score_to_students(a, visibility)
    ]
    assignment_ids = [
        str(a.get("assignment_id", "")).strip()
        for a in visible_assignments
        if str(a.get("assignment_id", "")).strip()
    ]
    submission_index = get_submissions_index(
        classroom=classroom,
        assignment_ids=assignment_ids,
        student_line_user_ids=[student_line_user_id],
    )
    exam_scores = get_exam_scores_by_classroom(classroom)
    totals = calculate_student_total_score(
        student_line_user_id,
        visible_assignments,
        submission_index,
        exam_scores,
    )

    assignment_items = []
    for assignment in visible_assignments:
        aid = str(assignment.get("assignment_id", "")).strip()
        sub = submission_index.get((student_line_user_id, aid), {})
        score_value = score_from_submission(sub, assignment)
        assignment_items.append({
            "assignment_id": aid,
            "title": str(assignment.get("title", "")).strip(),
            "chapter_name": str(assignment.get("chapter_name", "")).strip(),
            "score_category": assignment.get("score_category", ""),
            "score_category_label": assignment.get("score_category_label", ""),
            "max_score": str(assignment.get("max_score", "")).strip(),
            "score": format_score_value(score_value) if score_value is not None else "",
            "teacher_comment": str(sub.get("teacher_comment", "")).strip(),
        })

    show_midterm = visibility.get("show_midterm_scores")
    show_final = visibility.get("show_final_scores")
    has_work = bool(visible_assignments)
    has_exam = show_midterm or show_final

    result = {
        "visibility": visibility,
        "assignments": assignment_items,
        "scores_visible": has_work or has_exam,
        "work": None,
        "exam": None,
        "total": None,
    }

    if not result["scores_visible"]:
        return result

    if has_work:
        result["work"] = {
            "coursework": format_score_value(totals.get("coursework", 0)),
            "assignment": format_score_value(totals.get("assignment", 0)),
            "quiz": format_score_value(totals.get("quiz", 0)),
            "notebook": format_score_value(totals.get("notebook", 0)),
        }

    exam_payload = {}
    if show_midterm:
        exam_payload["midterm"] = format_score_value(totals.get("midterm", 0))
    if show_final:
        exam_payload["final"] = format_score_value(totals.get("final", 0))
    if show_midterm and show_final:
        exam_payload["exam_total"] = format_score_value(
            totals.get("midterm", 0) + totals.get("final", 0)
        )
    if exam_payload:
        result["exam"] = exam_payload

    total_value = 0.0
    has_total = False
    if has_work:
        total_value += parse_float_value(totals.get("coursework", 0), 0)
        has_total = True
    if show_midterm:
        total_value += parse_float_value(totals.get("midterm", 0), 0)
        has_total = True
    if show_final:
        total_value += parse_float_value(totals.get("final", 0), 0)
        has_total = True
    if has_total:
        result["total"] = format_score_value(min(total_value, 100.0))

    return result


def get_submissions_index(classroom=None, assignment_ids=None, student_line_user_ids=None):
    classroom = str(classroom or "").strip()
    assignment_id_set = None
    if assignment_ids is not None:
        assignment_id_set = {str(aid).strip() for aid in assignment_ids if str(aid).strip()}

    student_id_set = None
    if student_line_user_ids is not None:
        student_id_set = {str(sid).strip() for sid in student_line_user_ids if str(sid).strip()}

    records = get_sheet_records("submissions")
    if db_enabled() and student_id_set is not None:
        for sid in student_id_set:
            records.extend(get_submissions_from_db(sid))

    result = {}
    for r in records:
        row_classroom = str(r.get("classroom", "")).strip()
        if classroom and row_classroom != classroom:
            continue

        sid = str(r.get("student_line_user_id", "")).strip()
        aid = str(r.get("assignment_id", "")).strip()
        if not sid or not aid:
            continue

        if student_id_set is not None and sid not in student_id_set:
            continue

        if assignment_id_set is not None and aid not in assignment_id_set:
            continue

        result[(sid, aid)] = r

    return result


def get_submission(student_line_user_id, assignment_id):
    records = get_sheet_records("submissions")

    for r in records:
        if (
            str(r.get("student_line_user_id", "")).strip() == str(student_line_user_id).strip()
            and str(r.get("assignment_id", "")).strip() == str(assignment_id).strip()
        ):
            return r

    return None


def find_submission_row(student_line_user_id, assignment_id):
    records = get_sheet_records("submissions")

    for row_i, r in enumerate(records, start=2):
        if (
            str(r.get("student_line_user_id", "")).strip() == str(student_line_user_id).strip()
            and str(r.get("assignment_id", "")).strip() == str(assignment_id).strip()
        ):
            return row_i, r

    return None, None


def parse_date(date_text):
    if not date_text:
        return None

    date_text = str(date_text).strip()

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_text, fmt).date()
        except Exception:
            pass

    return None


def assignment_due_datetime(assignment):
    due = parse_date(str(assignment.get("due_date", "")).strip() if assignment else "")
    if not due:
        return None

    due_time_text = assignment_due_time(assignment)
    due_hour, due_minute = [int(part) for part in due_time_text.split(":", 1)]
    return datetime.combine(due, dt_time(due_hour, due_minute))


def submission_edit_allowed(assignment):
    due_dt = assignment_due_datetime(assignment)
    if not due_dt:
        return False

    return now_dt().replace(tzinfo=None) <= due_dt


def is_late_submission(due_date_text, submitted_at_text=None, due_time_text=None):
    due = parse_date(due_date_text)
    if not due:
        return "ไม่ทราบ"

    due_time_text = normalize_time_text(due_time_text, DEFAULT_DUE_TIME)
    due_hour, due_minute = [int(part) for part in due_time_text.split(":", 1)]
    due_dt = datetime.combine(due, dt_time(due_hour, due_minute))

    if submitted_at_text:
        try:
            submitted_dt = datetime.strptime(submitted_at_text, "%Y-%m-%d %H:%M:%S")
        except Exception:
            submitted_dt = now_dt().replace(tzinfo=None)
    else:
        submitted_dt = now_dt().replace(tzinfo=None)

    return "ใช่" if submitted_dt > due_dt else "ไม่ใช่"


def format_score_number(value):
    try:
        value = float(value)
    except Exception:
        return ""

    if value.is_integer():
        return str(int(value))

    return f"{value:.2f}".rstrip("0").rstrip(".")


def late_submission_days(due_date_text, submitted_at_text=None, due_time_text=None):
    due = parse_date(due_date_text)
    if not due:
        return 0

    due_time_text = normalize_time_text(due_time_text, DEFAULT_DUE_TIME)
    due_hour, due_minute = [int(part) for part in due_time_text.split(":", 1)]
    due_dt = datetime.combine(due, dt_time(due_hour, due_minute))

    if submitted_at_text:
        try:
            submitted_dt = datetime.strptime(submitted_at_text, "%Y-%m-%d %H:%M:%S")
        except Exception:
            submitted_dt = now_dt().replace(tzinfo=None)
    else:
        submitted_dt = now_dt().replace(tzinfo=None)

    if submitted_dt <= due_dt:
        return 0

    late_seconds = (submitted_dt - due_dt).total_seconds()
    return max(1, int((late_seconds + 86400 - 1) // 86400))


def calculate_auto_submission_score(assignment, submitted_at_text=None):
    try:
        max_score = float(str(assignment.get("max_score", "")).strip())
    except Exception:
        max_score = 0.0

    if max_score <= 0:
        weight = parse_assignment_weight(assignment.get("score_weight", ""))
        if weight and weight > 0:
            max_score = weight
        else:
            return ""

    due_date = str(assignment.get("due_date", "")).strip()
    due_time = assignment_due_time(assignment)
    late_days = late_submission_days(due_date, submitted_at_text, due_time)
    reduction_percent = min(late_days * 10, 50)
    score = max_score * (100 - reduction_percent) / 100
    return format_score_number(score)


# =========================================================
# Google Drive Helpers
# =========================================================

def escape_drive_query_value(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def clean_drive_name(value, fallback="ไม่ระบุชื่อ"):
    name = str(value or "").strip()
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or fallback


def find_or_create_drive_folder(folder_name, parent_id):
    folder_name = clean_drive_name(folder_name, "โฟลเดอร์")
    folder_lock = get_operation_lock(("drive_folder", parent_id, folder_name))
    with folder_lock:
        service = get_drive_service()
        folder_name_for_query = escape_drive_query_value(folder_name)
        parent_id_for_query = escape_drive_query_value(parent_id)

        query = (
            f"mimeType='application/vnd.google-apps.folder' "
            f"and name='{folder_name_for_query}' "
            f"and '{parent_id_for_query}' in parents "
            f"and trashed=false"
        )

        res = call_google_sheet_api(
            lambda: service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        )

        files = res.get("files", [])
        if files:
            return files[0]["id"]

        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }

        folder = call_google_sheet_api(
            lambda: service.files().create(
                body=metadata,
                fields="id",
                supportsAllDrives=True,
            ).execute()
        )

        return folder["id"]


def drive_file_id_from_url(url):
    url = str(url or "").strip()
    if not url:
        return ""

    patterns = [
        r"/file/d/([^/?#]+)",
        r"[?&]id=([^&#]+)",
        r"/open\?id=([^&#]+)",
        r"/uc\?id=([^&#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return ""


def update_drive_file(file_id, file_bytes=None, file_name=None, file_stream=None):
    file_id = str(file_id or "").strip()
    if not file_id:
        return ""

    service = get_drive_service()
    safe_file_name = clean_drive_name(file_name, "upload_file")
    mime_type, _ = mimetypes.guess_type(safe_file_name)
    if not mime_type:
        mime_type = "application/octet-stream"

    # Accept either raw bytes or a file-like stream to avoid loading large files into memory
    source = None
    if file_stream is not None:
        try:
            file_stream.seek(0)
        except Exception:
            pass
        source = file_stream
    else:
        source = BytesIO(file_bytes or b"")

    media = MediaIoBaseUpload(
        source,
        mimetype=mime_type,
        resumable=False,
    )

    file = call_google_sheet_api(
        lambda: service.files().update(
            fileId=file_id,
            body={"name": safe_file_name},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
    )

    return file.get("webViewLink", "")


def upload_file_to_drive(file_bytes=None, file_name=None, classroom=None, assignment_title=None, file_stream=None):
    if not GOOGLE_DRIVE_ROOT_FOLDER_ID:
        raise ValueError("ยังไม่ได้ตั้งค่า GOOGLE_DRIVE_ROOT_FOLDER_ID หรือ GOOGLE_DRIVE_FOLDER_ID")

    service = get_drive_service()
    classroom_name = clean_drive_name(classroom, "ไม่ระบุห้อง")
    assignment_folder_name = clean_drive_name(assignment_title, "ไม่ระบุชื่องาน")
    safe_file_name = clean_drive_name(file_name, "upload_file")

    classroom_folder_id = find_or_create_drive_folder(
        f"ห้อง {classroom_name}",
        GOOGLE_DRIVE_ROOT_FOLDER_ID,
    )
    assignment_folder_id = find_or_create_drive_folder(
        assignment_folder_name,
        classroom_folder_id,
    )

    mime_type, _ = mimetypes.guess_type(safe_file_name)
    if not mime_type:
        mime_type = "application/octet-stream"

    # Accept either a file-like stream or raw bytes to avoid keeping the whole file in memory
    source = None
    if file_stream is not None:
        try:
            file_stream.seek(0)
        except Exception:
            pass
        source = file_stream
    else:
        source = BytesIO(file_bytes or b"")

    media = MediaIoBaseUpload(
        source,
        mimetype=mime_type,
        resumable=False,
    )

    metadata = {
        "name": safe_file_name,
        "parents": [assignment_folder_id],
    }

    uploaded = call_google_sheet_api(
        lambda: service.files().create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
    )

    file_id = uploaded["id"]

    try:
        call_google_sheet_api(
            lambda: service.permissions().create(
                fileId=file_id,
                body={
                    "type": "anyone",
                    "role": "reader",
                },
                supportsAllDrives=True,
            ).execute()
        )
    except Exception as e:
        print("[drive permission] Error:", e)

    file = call_google_sheet_api(
        lambda: service.files().get(
            fileId=file_id,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
    )

    return file.get("webViewLink", "")


# =========================================================
# Pages
# =========================================================

@app.route("/")
def index():
    return "LINE School Bot is running."


@app.route("/health")
@app.route("/healthz")
def health_check():
    return jsonify({
        "success": True,
        "status": "ok",
        "service": "line-school-bot",
        "time": now_text(),
    })


# ---------- Student Pages ----------

@app.route("/student-register")
def student_register_page():
    return render_liff_template("student_register.html", LIFF_STUDENT_REGISTER_ID)


@app.route("/student-submit")
def student_submit_page():
    return render_liff_template("student_submit.html", LIFF_STUDENT_SUBMIT_ID)


@app.route("/student-pending")
def student_pending_page():
    return render_liff_template("student_pending.html", LIFF_STUDENT_PENDING_ID)


@app.route("/student-question")
def student_question_page():
    return render_liff_template("student_question.html", LIFF_STUDENT_QUESTION_ID)


@app.route("/student-announce")
def student_announce_page():
    return render_liff_template("student_announce.html", LIFF_STUDENT_ANNOUNCE_ID)


@app.route("/student-scores")
def student_scores_page():
    return render_liff_template("student_scores.html", LIFF_STUDENT_ANNOUNCE_ID)


# ---------- Teacher Pages ----------

@app.route("/teacher-setup")
def teacher_setup_page():
    return render_liff_template("teacher_setup.html", LIFF_TEACHER_SETUP_ID)


@app.route("/teacher-assignment")
def teacher_assignment_page():
    if request.args.get("mode", "").strip() == "scores":
        return render_liff_template("teacher_assignment_scores.html", LIFF_TEACHER_ASSIGNMENT_ID)
    return render_liff_template("teacher_assignment.html", LIFF_TEACHER_ASSIGNMENT_ID)


@app.route("/teacher-pending")
def teacher_pending_page():
    return render_liff_template("teacher_pending.html", LIFF_TEACHER_PENDING_ID)


@app.route("/teacher-questions")
def teacher_questions_page():
    return render_liff_template("teacher_questions.html", LIFF_TEACHER_QUESTIONS_ID)


@app.route("/teacher-announce")
def teacher_announce_page():
    return render_liff_template("teacher_announce.html", LIFF_TEACHER_ANNOUNCE_ID)


# =========================================================
# Debug
# =========================================================

@app.route("/debug/env")
def debug_env():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    return jsonify({
        "LINE_CHANNEL_ACCESS_TOKEN": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "LINE_CHANNEL_SECRET": bool(LINE_CHANNEL_SECRET),

        "GOOGLE_SHEET_ID": bool(GOOGLE_SHEET_ID),
        "GOOGLE_DRIVE_ROOT_FOLDER_ID": bool(GOOGLE_DRIVE_ROOT_FOLDER_ID),
        "GOOGLE_DRIVE_FOLDER_ID": bool(os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()),
        "GOOGLE_AUTH_MODE": "oauth" if (
            os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
            and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
            and os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
        ) else "service_account",
        "GOOGLE_OAUTH_CLIENT_ID": bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()),
        "GOOGLE_OAUTH_CLIENT_SECRET": bool(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()),
        "GOOGLE_OAUTH_REFRESH_TOKEN": bool(os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()),
        "GOOGLE_SERVICE_ACCOUNT_JSON": bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()),
        "GOOGLE_CREDENTIALS_JSON": bool(os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()),
        "credentials_json_file_exists": os.path.exists("credentials.json"),

        "CRON_SECRET": bool(CRON_SECRET),
        "DATABASE_URL": db_enabled(),
        "TEACHER_SETUP_CODE": bool(TEACHER_SETUP_CODE),

        "LIFF_STUDENT_REGISTER_ID": bool(LIFF_STUDENT_REGISTER_ID),
        "LIFF_STUDENT_SUBMIT_ID": bool(LIFF_STUDENT_SUBMIT_ID),
        "LIFF_STUDENT_PENDING_ID": bool(LIFF_STUDENT_PENDING_ID),
        "LIFF_STUDENT_QUESTION_ID": bool(LIFF_STUDENT_QUESTION_ID),
        "LIFF_STUDENT_ANNOUNCE_ID": bool(LIFF_STUDENT_ANNOUNCE_ID),

        "LIFF_TEACHER_SETUP_ID": bool(LIFF_TEACHER_SETUP_ID),
        "LIFF_TEACHER_ASSIGNMENT_ID": bool(LIFF_TEACHER_ASSIGNMENT_ID),
        "LIFF_TEACHER_PENDING_ID": bool(LIFF_TEACHER_PENDING_ID),
        "LIFF_TEACHER_QUESTIONS_ID": bool(LIFF_TEACHER_QUESTIONS_ID),
        "LIFF_TEACHER_ANNOUNCE_ID": bool(LIFF_TEACHER_ANNOUNCE_ID),

        "STUDENT_RICH_MENU_REGISTER_ID": bool(STUDENT_RICH_MENU_REGISTER_ID),
        "STUDENT_RICH_MENU_NORMAL_ID": bool(STUDENT_RICH_MENU_NORMAL_ID),
        "STUDENT_RICH_MENU_PENDING_ALERT_ID": bool(STUDENT_RICH_MENU_PENDING_ALERT_ID),
        "STUDENT_RICH_MENU_ANSWER_ALERT_ID": bool(STUDENT_RICH_MENU_ANSWER_ALERT_ID),
        "STUDENT_RICH_MENU_BOTH_ALERT_ID": bool(STUDENT_RICH_MENU_BOTH_ALERT_ID),

        "TEACHER_RICH_MENU_SETUP_ID": bool(TEACHER_RICH_MENU_SETUP_ID),
        "TEACHER_RICH_MENU_NORMAL_ID": bool(TEACHER_RICH_MENU_NORMAL_ID),
        "TEACHER_RICH_MENU_QUESTION_ALERT_ID": bool(TEACHER_RICH_MENU_QUESTION_ALERT_ID),
    })


@app.route("/debug/teacher-rooms")
def debug_teacher_rooms():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    teacher_line_user_id = (
        request.args.get("teacher_line_user_id", "")
        or request.args.get("line_user_id", "")
        or request.args.get("user_id", "")
    ).strip()

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)
    if not teacher:
        return jsonify({
            "success": False,
            "message": "ไม่พบครูจาก user id นี้",
            "teacher_line_user_id": teacher_line_user_id,
        })

    rooms_text = first_record_value(teacher, [
        "rooms",
        "room",
        "classroom",
        "classrooms",
    ])

    return jsonify({
        "success": True,
        "teacher_line_user_id": teacher_line_user_id,
        "teacher_name": first_record_value(teacher, ["teacher_name", "full_name", "name"]),
        "rooms_text": rooms_text,
        "rooms": normalize_rooms_text(rooms_text),
        "teacher_record_keys": list(teacher.keys()),
    })



@app.route("/debug/liff")
def debug_liff():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    def mask(v):
        v = clean_liff_id(v)
        if not v:
            return ""
        if len(v) <= 8:
            return v
        return v[:6] + "..." + v[-4:]

    return jsonify({
        "LIFF_STUDENT_REGISTER_ID": mask(LIFF_STUDENT_REGISTER_ID),
        "LIFF_STUDENT_SUBMIT_ID": mask(LIFF_STUDENT_SUBMIT_ID),
        "LIFF_STUDENT_PENDING_ID": mask(LIFF_STUDENT_PENDING_ID),
        "LIFF_STUDENT_QUESTION_ID": mask(LIFF_STUDENT_QUESTION_ID),
        "LIFF_STUDENT_ANNOUNCE_ID": mask(LIFF_STUDENT_ANNOUNCE_ID),
        "LIFF_TEACHER_SETUP_ID": mask(LIFF_TEACHER_SETUP_ID),
        "LIFF_TEACHER_ASSIGNMENT_ID": mask(LIFF_TEACHER_ASSIGNMENT_ID),
        "LIFF_TEACHER_PENDING_ID": mask(LIFF_TEACHER_PENDING_ID),
        "LIFF_TEACHER_QUESTIONS_ID": mask(LIFF_TEACHER_QUESTIONS_ID),
        "LIFF_TEACHER_ANNOUNCE_ID": mask(LIFF_TEACHER_ANNOUNCE_ID),
    })


@app.route("/debug/line-profile/<user_id>")
def debug_line_profile(user_id):
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    url = f"https://api.line.me/v2/bot/profile/{user_id}"

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    try:
        r = requests.get(url, headers=headers, timeout=15)
        return jsonify({
            "success": r.status_code == 200,
            "status_code": r.status_code,
            "response": r.json() if r.text else {},
            "raw": r.text,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e),
        })

@app.route("/debug/setup-sheets")
def debug_setup_sheets():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    result = setup_base_sheets()
    result.update({
        "success": True,
        "message": "setup sheets completed",
    })
    return jsonify(result)


@app.route("/debug/create-new-spreadsheet")
def debug_create_new_spreadsheet():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    title = request.args.get("title", "").strip()
    result = create_fresh_base_spreadsheet(title)
    result.update({
        "success": True,
        "message": "created new spreadsheet; copy spreadsheet_id to GOOGLE_SHEET_ID and redeploy",
    })
    return jsonify(result)


@app.route("/debug/prune-sheets")
def debug_prune_sheets():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    confirm = request.args.get("confirm", "").strip()
    if confirm != "delete-extra":
        return jsonify({
            "success": False,
            "message": "add confirm=delete-extra to delete sheets that are not used by the bot",
            "required_sheets": list(BASE_SHEETS.keys()),
        }), 400

    keep_classroom_sheets = request.args.get("keep_classroom_sheets", "1").strip() != "0"
    result = prune_current_spreadsheet(keep_classroom_sheets=keep_classroom_sheets)
    result.update({
        "success": True,
        "message": "current spreadsheet pruned and required bot sheets are ready",
        "keep_classroom_sheets": keep_classroom_sheets,
    })
    return jsonify(result)


@app.route("/debug/update-classroom/<classroom>")
def debug_update_classroom(classroom):
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    classroom = normalize_classroom_text(classroom)
    create_or_update_classroom_sheet(classroom)
    create_or_update_attendance_sheet(classroom)
    return jsonify({
        "success": True,
        "message": f"อัปเดตชีตห้อง {classroom} เรียบร้อยแล้ว",
    })


@app.route("/debug/reset-all-richmenu")
def debug_reset_all_richmenu():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    updated_students = 0
    updated_teachers = 0

    try:
        students = get_sheet_records("students")

        for s in students:
            user_id = str(s.get("student_line_user_id", "")).strip()
            if user_id:
                unlink_rich_menu_from_user(user_id)
                update_student_rich_menu(user_id)
                updated_students += 1
    except Exception as e:
        print("[reset students richmenu] Error:", e)

    try:
        teachers = get_sheet_records("teachers")

        for t in teachers:
            user_id = str(t.get("teacher_line_user_id", "")).strip()
            if user_id:
                unlink_rich_menu_from_user(user_id)
                update_teacher_rich_menu(user_id)
                updated_teachers += 1
    except Exception as e:
        print("[reset teachers richmenu] Error:", e)

    return jsonify({
        "success": True,
        "students_updated": updated_students,
        "teachers_updated": updated_teachers,
    })


@app.route("/debug/update-richmenu/<user_id>")
def debug_update_richmenu(user_id):
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    teacher = get_teacher_by_line_user_id(user_id)
    student = get_student_by_line_user_id(user_id)

    if teacher:
        res = update_teacher_rich_menu(user_id)
        return jsonify({
            "success": True,
            "type": "teacher",
            "line": line_response_summary(res),
        })

    if student:
        res = update_student_rich_menu(user_id)
        return jsonify({
            "success": True,
            "type": "student",
            "line": line_response_summary(res),
        })

    unlink_rich_menu_from_user(user_id)
    res = update_student_rich_menu(user_id)
    return jsonify({
        "success": True,
        "type": "guest",
        "line": line_response_summary(res),
    })


# =========================================================
# API: Teacher
# =========================================================

@app.route("/api/teacher/setup", methods=["POST"])
def api_teacher_setup():
    try:
        data = request.get_json()

        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        teacher_name = str(
            data.get("teacher_name", "") or data.get("full_name", "")
        ).strip()
        rooms = str(data.get("rooms", "")).strip()
        teacher_code = str(data.get("teacher_code", "")).strip()

        if not teacher_line_user_id or not teacher_name or not rooms or not teacher_code:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกชื่อครู รหัสครู และห้องที่ดูแลให้ครบ",
            })

        if not TEACHER_SETUP_CODE:
            return jsonify({
                "success": False,
                "message": "ระบบยังไม่ได้ตั้งค่า TEACHER_SETUP_CODE ใน Render",
            })

        if teacher_code != TEACHER_SETUP_CODE:
            return jsonify({
                "success": False,
                "message": "รหัสครูไม่ถูกต้อง",
            })

        room_list = normalize_rooms_text(rooms)
        clean_rooms = " ".join(room_list)

        if not room_list:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกห้องที่ดูแล เช่น 401 402",
            })

        ws = get_worksheet("teachers")
        ensure_headers(ws, BASE_SHEETS["teachers"])
        records = get_sheet_records("teachers")
        headers = get_sheet_headers("teachers")

        for i, r in enumerate(records, start=2):
            row_user_id = first_record_value(r, [
                "teacher_line_user_id",
                "line_user_id",
                "user_id",
            ])
            if row_user_id == teacher_line_user_id:
                existing_rooms = first_record_value(r, [
                    "rooms",
                    "room",
                    "classroom",
                    "classrooms",
                ])
                existing_room_list = normalize_rooms_text(existing_rooms)
                merged_room_list = existing_room_list[:]
                for room in room_list:
                    if room not in merged_room_list:
                        merged_room_list.append(room)

                clean_rooms = " ".join(merged_room_list)

                teacher_updates = []
                for header, value in {
                    "teacher_line_user_id": teacher_line_user_id,
                    "line_user_id": teacher_line_user_id,
                    "user_id": teacher_line_user_id,
                    "teacher_name": teacher_name,
                    "full_name": teacher_name,
                    "rooms": clean_rooms,
                }.items():
                    if header in headers:
                        teacher_updates.append({
                            "range": f"{col_letter(headers.index(header) + 1)}{i}",
                            "values": [[value]],
                        })
                if teacher_updates:
                    ws.batch_update(teacher_updates, value_input_option="RAW")
                    invalidate_sheet_cache("teachers")

                for room in merged_room_list:
                    mark_room_dirty(room, "teacher_setup")

                try:
                    update_teacher_rich_menu(teacher_line_user_id)
                except Exception as e:
                    print("[teacher setup richmenu] Error:", e)

                return jsonify({
                    "success": True,
                    "message": f"อัปเดตข้อมูลครูเรียบร้อยแล้ว ห้องที่ดูแลตอนนี้: {clean_rooms}",
                    "rooms": merged_room_list,
                })

        buffered_append_row(
            ws,
            row_values_for_headers(headers, {
                "teacher_line_user_id": teacher_line_user_id,
                "line_user_id": teacher_line_user_id,
                "user_id": teacher_line_user_id,
                "teacher_name": teacher_name,
                "full_name": teacher_name,
                "rooms": clean_rooms,
                "created_at": now_text(),
            }),
            value_input_option="RAW",
        )
        invalidate_sheet_cache("teachers")

        for room in room_list:
            mark_room_dirty(room, "teacher_setup")

        try:
            update_teacher_rich_menu(teacher_line_user_id)
        except Exception as e:
            print("[teacher setup richmenu] Error:", e)

        return jsonify({
            "success": True,
            "message": "บันทึกข้อมูลครูเรียบร้อยแล้ว",
            "rooms": room_list,
        })

    except Exception as e:
        print("[api_teacher_setup] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/my-rooms")
def api_teacher_my_rooms():
    teacher_line_user_id = (
        request.args.get("teacher_line_user_id", "")
        or request.args.get("line_user_id", "")
    ).strip()

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)
    if not teacher:
        return jsonify({
            "success": False,
            "message": "ไม่มีสิทธิ์ครู",
        })

    return jsonify({
        "success": True,
        "teacher": teacher,
        "rooms": get_teacher_rooms(teacher_line_user_id),
    })


@app.route("/api/teacher/assignment", methods=["POST"])
def api_teacher_assignment():
    try:
        data = request.get_json()

        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        chapter_name = str(data.get("chapter_name", "")).strip()
        work_title = str(data.get("work_title", "") or data.get("title", "")).strip()
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        start_date = str(data.get("start_date", "")).strip()
        due_date = str(data.get("due_date", "")).strip()
        due_time = normalize_time_text(data.get("due_time", ""), DEFAULT_DUE_TIME)
        max_score = str(data.get("max_score", "")).strip()
        score_category = normalize_score_category(data.get("score_category", "assignment"))
        score_weight = str(data.get("score_weight", "")).strip()
        allowed_file_exts = normalize_allowed_file_exts(
            data.get("allowed_file_types", [])
            or data.get("allowed_file_exts", [])
        )
        allow_link_submission = parse_bool(data.get("allow_link_submission", True), default=True)

        if not title:
            title = build_assignment_title(chapter_name, score_category, work_title)

        if score_category == "quiz":
            allowed_file_exts = []
            allow_link_submission = False
            if not due_date:
                due_date = datetime.now().strftime("%Y-%m-%d")
            if not description:
                description = SCORE_CATEGORIES.get(score_category, "")

        teacher, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณากรอกห้อง ชื่องาน และวันสิ้นสุด",
        )
        if access_error:
            return jsonify({
                "success": False,
                "message": access_error,
            })

        if not classroom or not title or not due_date:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกห้อง ชื่อบท และข้อมูลให้ครบ",
            })

        if not chapter_name:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกชื่อบท",
            })

        if score_category == "assignment" and not work_title:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกชื่องาน/ใบงาน",
            })

        score_weight_value = parse_assignment_weight(score_weight)
        if score_weight_value is None:
            return jsonify({
                "success": False,
                "message": "น้ำหนักคะแนนต้องเป็นตัวเลข",
            })

        if score_weight_value < 0:
            return jsonify({
                "success": False,
                "message": "น้ำหนักคะแนนต้องไม่ติดลบ",
            })

        if score_weight_value > 60:
            return jsonify({
                "success": False,
                "message": "น้ำหนักคะแนนต้องไม่เกิน 60",
            })

        max_score_value = 0.0
        if score_weight_value > 0:
            if not max_score:
                max_score_value = score_weight_value
            else:
                try:
                    max_score_value = float(max_score)
                except Exception:
                    return jsonify({
                        "success": False,
                        "message": "คะแนนเต็มต้องเป็นตัวเลข",
                    })

                if max_score_value <= 0:
                    return jsonify({
                        "success": False,
                        "message": "คะแนนเต็มต้องมากกว่า 0",
                    })

                if max_score_value > 1000:
                    return jsonify({
                        "success": False,
                        "message": "คะแนนเต็มต้องไม่เกิน 1000",
                    })
        elif max_score:
            try:
                max_score_value = float(max_score)
            except Exception:
                return jsonify({
                    "success": False,
                    "message": "คะแนนเต็มต้องเป็นตัวเลข",
                })
            if max_score_value <= 0:
                return jsonify({
                    "success": False,
                    "message": "คะแนนเต็มต้องมากกว่า 0",
                })
            if max_score_value > 1000:
                return jsonify({
                    "success": False,
                    "message": "คะแนนเต็มต้องไม่เกิน 1000",
                })

        if not allowed_file_exts and not allow_link_submission:
            return jsonify({
                "success": False,
                "message": "กรุณาเลือกประเภทไฟล์ หรืออนุญาตให้ส่งเป็นลิงก์อย่างน้อย 1 อย่าง",
            })

        assignment_id = "as_" + uuid.uuid4().hex[:12]
        teacher_name = first_record_value(teacher, ["teacher_name", "full_name", "name"])

        ws = get_worksheet("assignments")
        headers = ensure_headers(ws, BASE_SHEETS["assignments"])

        buffered_append_row(
            ws,
            row_values_for_headers(headers, {
                "assignment_id": assignment_id,
                "created_at": now_text(),
                "teacher_line_user_id": teacher_line_user_id,
                "line_user_id": teacher_line_user_id,
                "teacher_name": teacher_name,
                "full_name": teacher_name,
                "classroom": classroom,
                "chapter_name": chapter_name,
                "title": title,
                "description": description,
                "start_date": start_date,
                "due_date": due_date,
                "due_time": due_time,
                "max_score": format_score_value(max_score_value) if max_score_value > 0 else "",
                "score_category": score_category,
                "score_weight": format_score_value(score_weight_value),
                "show_score_to_students": "no",
                "allowed_file_types": ",".join(allowed_file_exts),
                "allow_link_submission": "yes" if allow_link_submission else "no",
            }),
            value_input_option="RAW",
        )

        add_assignment_header_to_classroom_sheet(classroom, title)

        # อัปเดต Rich Menu นักเรียนในห้องนั้น ถ้าเคยแอดบอทไว้
        students = get_students_by_classroom(classroom)
        for s in students:
            sid = str(s.get("student_line_user_id", "")).strip()
            if sid:
                update_student_rich_menu(sid)

        return jsonify({
            "success": True,
            "message": "บันทึกงานเรียบร้อยแล้ว",
            "assignment_id": assignment_id,
            "score_category": score_category,
            "redirect_to_scores": score_category == "quiz",
        })

    except Exception as e:
        print("[api_teacher_assignment] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/chapters")
def api_teacher_chapters():
    try:
        teacher_line_user_id = str(
            request.args.get("teacher_line_user_id", "") or request.args.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        return jsonify({
            "success": True,
            "chapters": get_chapter_names_by_classroom(classroom),
        })

    except Exception as e:
        print("[api_teacher_chapters] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/assignments", methods=["GET"])
def api_teacher_assignments():
    try:
        teacher_line_user_id = str(
            request.args.get("teacher_line_user_id", "") or request.args.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        assignments = get_assignments_by_classroom(classroom)
        result = []
        for a in assignments:
            assignment_with_meta = add_assignment_due_metadata(dict(a))
            result.append({
                "assignment_id": assignment_with_meta.get("assignment_id", ""),
                "title": assignment_with_meta.get("title", ""),
                "chapter_name": assignment_with_meta.get("chapter_name", ""),
                "score_category": assignment_with_meta.get("score_category", ""),
                "score_category_label": assignment_with_meta.get("score_category_label", ""),
                "score_weight": assignment_with_meta.get("score_weight", ""),
            })

        return jsonify({
            "success": True,
            "assignments": result,
        })

    except Exception as e:
        print("[api_teacher_assignments] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/assignment-scores", methods=["GET"])
def api_teacher_assignment_scores_get():
    try:
        teacher_line_user_id = str(
            request.args.get("teacher_line_user_id", "") or request.args.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))
        assignment_id = str(request.args.get("assignment_id", "")).strip()

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        if not assignment_id:
            return jsonify({
                "success": False,
                "message": "ไม่พบรหัสงาน",
            })

        assignment = get_assignment_by_id(assignment_id)
        if not assignment:
            return jsonify({
                "success": False,
                "message": "ไม่พบงานที่เลือก",
            })

        if normalize_classroom_text(assignment.get("classroom", "")) != classroom:
            return jsonify({
                "success": False,
                "message": "งานนี้ไม่ได้อยู่ในห้องที่เลือก",
            })

        student_line_user_ids = [
            str(s.get("student_line_user_id", "")).strip()
            for s in get_students_by_classroom(classroom)
            if str(s.get("student_line_user_id", "")).strip()
        ]
        submission_index = get_submissions_index(
            classroom=classroom,
            assignment_ids=[assignment_id],
            student_line_user_ids=student_line_user_ids,
        )

        students = []
        for student in get_students_by_classroom(classroom):
            sid = str(student.get("student_line_user_id", "")).strip()
            sub = submission_index.get((sid, assignment_id), {})
            score_text = str(sub.get("score", "")).strip()
            if score_text == "":
                score_text = str(sub.get("auto_score", "")).strip()
            students.append({
                "student_line_user_id": sid,
                "student_name": str(student.get("student_name", "")).strip(),
                "student_code": str(student.get("student_code", "")).strip(),
                "score": score_text,
                "teacher_comment": str(sub.get("teacher_comment", "")).strip(),
            })

        return jsonify({
            "success": True,
            "assignment": {
                "assignment_id": assignment_id,
                "title": str(assignment.get("title", "")).strip(),
                "chapter_name": str(assignment.get("chapter_name", "")).strip(),
                "max_score": str(assignment.get("max_score", "")).strip(),
                "score_category": assignment.get("score_category", ""),
                "score_category_label": assignment.get("score_category_label", ""),
            },
            "students": students,
        })

    except Exception as e:
        print("[api_teacher_assignment_scores_get] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/assignment-scores", methods=["POST"])
def api_teacher_assignment_scores_post():
    try:
        data = request.get_json() or {}
        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        assignment_id = str(data.get("assignment_id", "")).strip()
        students = data.get("students") or []

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        if not assignment_id:
            return jsonify({
                "success": False,
                "message": "ไม่พบรหัสงาน",
            })

        assignment = get_assignment_by_id(assignment_id)
        if not assignment:
            return jsonify({
                "success": False,
                "message": "ไม่พบงานที่เลือก",
            })

        if normalize_classroom_text(assignment.get("classroom", "")) != classroom:
            return jsonify({
                "success": False,
                "message": "งานนี้ไม่ได้อยู่ในห้องที่เลือก",
            })

        if not isinstance(students, list) or not students:
            return jsonify({
                "success": False,
                "message": "ยังไม่มีรายชื่อนักเรียนสำหรับบันทึกคะแนน",
            })

        max_score_value = parse_float_value(assignment.get("max_score", ""), 0)
        assignment_title = str(assignment.get("title", "")).strip()
        ws = get_worksheet("submissions")
        headers = ensure_headers(ws, BASE_SHEETS["submissions"])
        checked_at = now_text()
        updates = []
        appended = 0
        updated_rows = set()

        for student in students:
            sid = str(student.get("student_line_user_id", "")).strip()
            if not sid:
                continue

            score_text = str(student.get("score", "")).strip()
            teacher_comment = str(student.get("teacher_comment", "")).strip()
            if score_text == "" and teacher_comment == "":
                continue

            if score_text != "":
                score_value = parse_float_value(score_text, None)
                if score_value is None:
                    return jsonify({
                        "success": False,
                        "message": "คะแนนต้องเป็นตัวเลข",
                    })
                if score_value < 0:
                    return jsonify({
                        "success": False,
                        "message": "คะแนนต้องไม่ติดลบ",
                    })
                if max_score_value > 0 and score_value > max_score_value:
                    return jsonify({
                        "success": False,
                        "message": f"คะแนนต้องไม่เกิน {format_score_value(max_score_value)}",
                    })
                score_text = format_score_value(score_value)

            row_i, existing = find_submission_row(sid, assignment_id)
            if existing:
                updated_rows.add(row_i)
                row_values = {
                    "student_name": str(student.get("student_name", "")).strip(),
                    "student_code": str(student.get("student_code", "")).strip(),
                    "score": score_text,
                    "teacher_comment": teacher_comment,
                    "checked_at": checked_at,
                    "checked_by": teacher_line_user_id,
                }
                for header, value in row_values.items():
                    if header in headers:
                        updates.append({
                            "range": f"{col_letter(headers.index(header) + 1)}{row_i}",
                            "values": [[value]],
                        })
            else:
                buffered_append_row(
                    ws,
                    row_values_for_headers(headers, {
                        "submission_id": "sub_" + uuid.uuid4().hex[:12],
                        "submitted_at": checked_at,
                        "assignment_id": assignment_id,
                        "assignment_title": assignment_title,
                        "student_line_user_id": sid,
                        "student_name": str(student.get("student_name", "")).strip(),
                        "student_code": str(student.get("student_code", "")).strip(),
                        "classroom": classroom,
                        "file_url": "",
                        "file_name": "ครูกรอกคะแนน",
                        "note": "",
                        "late": "no",
                        "auto_score": "",
                        "score": score_text,
                        "checked_at": checked_at,
                        "checked_by": teacher_line_user_id,
                        "teacher_comment": teacher_comment,
                    }),
                    value_input_option="USER_ENTERED",
                )
                appended += 1

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")

        invalidate_sheet_cache("submissions")
        mark_room_dirty(classroom, "assignment_scores_updated")

        return jsonify({
            "success": True,
            "message": "บันทึกคะแนนเรียบร้อยแล้ว",
            "count": len([s for s in students if str(s.get("student_line_user_id", "")).strip()]),
            "appended": appended,
            "updated": len(updated_rows),
        })

    except Exception as e:
        print("[api_teacher_assignment_scores_post] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/pending")
def api_teacher_pending():
    teacher_line_user_id = (
        request.args.get("teacher_line_user_id", "")
        or request.args.get("line_user_id", "")
    ).strip()
    classroom = normalize_classroom_text(request.args.get("classroom", "").strip())
    assignment_id = request.args.get("assignment_id", "").strip()

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)
    if not teacher:
        return jsonify({
            "success": False,
            "message": "ไม่มีสิทธิ์ครู",
        })

    rooms = get_teacher_rooms(teacher_line_user_id)
    if classroom and classroom not in rooms:
        return jsonify({
            "success": False,
            "message": "คุณไม่ได้ดูแลห้องนี้",
        })

    if not classroom:
        classroom = rooms[0] if rooms else ""

    assignments = get_assignments_by_classroom(classroom)

    if not assignment_id and assignments:
        assignment_id = str(assignments[0].get("assignment_id", "")).strip()

    students = get_students_by_classroom(classroom)
    student_line_user_ids = student_line_user_ids_for_records(students)
    submission_index = get_submissions_index(
        classroom=classroom,
        assignment_ids=[assignment_id] if assignment_id else [],
        student_line_user_ids=student_line_user_ids,
    )
    pending_students = []

    for s in students:
        sub = student_submission_from_index(s, assignment_id, submission_index)
        if not sub:
            pending_students.append(s)

    return jsonify({
        "success": True,
        "rooms": rooms,
        "classroom": classroom,
        "assignments": assignments,
        "assignment_id": assignment_id,
        "pending_students": pending_students,
    })


def question_is_pinned(question):
    return parse_bool(question.get("is_pinned", ""), default=False)


def normalize_room_list_value(value):
    if isinstance(value, (list, tuple, set)):
        value = " ".join(str(v) for v in value)
    return normalize_rooms_text(value)


def question_pinned_classrooms(question):
    if not question_is_pinned(question):
        return []

    rooms = normalize_room_list_value(question.get("pinned_classrooms", ""))
    if rooms:
        return rooms

    classroom = normalize_classroom_text(question.get("classroom", ""))
    return [classroom] if classroom else []


def question_is_pinned_for_classroom(question, classroom):
    classroom = normalize_classroom_text(classroom)
    return bool(classroom and classroom in question_pinned_classrooms(question))


def parse_pin_target_rooms(data, default_classroom=""):
    value = data.get("pinned_classrooms", None)
    if value is None:
        value = data.get("pin_classrooms", None)
    if value is None:
        value = data.get("classrooms", None)

    rooms = normalize_room_list_value(value)
    if not rooms:
        default_classroom = normalize_classroom_text(default_classroom)
        rooms = [default_classroom] if default_classroom else []
    return rooms


def validate_pin_target_rooms(target_rooms, teacher_rooms):
    if not target_rooms:
        return "กรุณาเลือกห้องที่จะปักหมุด"

    denied_rooms = [room for room in target_rooms if room not in teacher_rooms]
    if denied_rooms:
        return "คุณไม่ได้ดูแลห้อง " + ", ".join(denied_rooms)

    return ""


def question_public_payload(question, classroom=None):
    pinned_classrooms = question_pinned_classrooms(question)
    source_classroom = normalize_classroom_text(question.get("classroom", ""))
    return {
        "question_id": str(question.get("question_id", "")).strip(),
        "created_at": str(question.get("created_at", "")).strip(),
        "classroom": classroom or source_classroom,
        "source_classroom": source_classroom,
        "question_text": str(question.get("question_text", "")).strip(),
        "attachment_url": str(question.get("attachment_url", "")).strip(),
        "attachment_name": str(question.get("attachment_name", "")).strip(),
        "answer_text": str(question.get("answer_text", "")).strip(),
        "answer_attachment_url": str(question.get("answer_attachment_url", "")).strip(),
        "answer_attachment_name": str(question.get("answer_attachment_name", "")).strip(),
        "answered_at": str(question.get("answered_at", "")).strip(),
        "is_pinned": question_is_pinned(question),
        "pinned_at": str(question.get("pinned_at", "")).strip(),
        "pinned_classrooms": pinned_classrooms,
        "pinned_classrooms_text": " ".join(pinned_classrooms),
    }


@app.route("/api/teacher/questions")
def api_teacher_questions():
    teacher_line_user_id = (
        request.args.get("teacher_line_user_id", "")
        or request.args.get("line_user_id", "")
    ).strip()
    classroom = normalize_classroom_text(request.args.get("classroom", "").strip())

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)
    if not teacher:
        return jsonify({
            "success": False,
            "message": "ไม่มีสิทธิ์ครู",
        })

    rooms = get_teacher_rooms(teacher_line_user_id)
    if classroom and classroom not in rooms:
        return jsonify({
            "success": False,
            "message": "คุณไม่ได้ดูแลห้องนี้",
            "rooms": rooms,
        })

    target_rooms = [classroom] if classroom else rooms

    ws = get_worksheet("questions")
    ensure_headers(ws, BASE_SHEETS["questions"])
    records = get_sheet_records("questions")

    result = []
    pinned_questions = []
    for r in records:
        row_classroom = normalize_classroom_text(r.get("classroom", ""))
        item = dict(r)
        item["classroom"] = row_classroom
        item["source_classroom"] = row_classroom
        item["is_pinned"] = question_is_pinned(item)
        item["pinned_classrooms"] = question_pinned_classrooms(item)
        item["pinned_classrooms_text"] = " ".join(item["pinned_classrooms"])
        item["attachment_url"] = str(r.get("attachment_url", "")).strip()
        item["attachment_name"] = str(r.get("attachment_name", "")).strip()
        item["answer_attachment_url"] = str(r.get("answer_attachment_url", "")).strip()
        item["answer_attachment_name"] = str(r.get("answer_attachment_name", "")).strip()

        if row_classroom in target_rooms and str(r.get("status", "")).strip() == "pending":
            result.append(item)

        if (
            item["is_pinned"]
            and str(r.get("status", "")).strip() == "answered"
            and str(r.get("answer_text", "")).strip()
            and any(room in target_rooms for room in item["pinned_classrooms"])
        ):
            pinned_questions.append(item)

    result.sort(key=lambda q: str(q.get("created_at", "")))
    pinned_questions.sort(
        key=lambda q: str(q.get("pinned_at", "")) or str(q.get("answered_at", "")),
        reverse=True,
    )

    return jsonify({
        "success": True,
        "rooms": rooms,
        "classroom": classroom,
        "questions": result,
        "pinned_questions": pinned_questions[:50],
    })


@app.route("/api/teacher/answer-question", methods=["POST"])
def api_teacher_answer_question():
    try:
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            data = request.form
        else:
            data = request.get_json() or {}

        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        question_id = str(data.get("question_id", "")).strip()
        answer_text = str(data.get("answer_text", "")).strip()
        answer_attachment_url = str(
            data.get("answer_attachment_url", "") or data.get("attachment_url", "") or data.get("file_url", "")
        ).strip()
        answer_attachment_name = ""
        pin_question = parse_bool(data.get("pin_question", False), default=False)

        teacher = get_teacher_by_line_user_id(teacher_line_user_id)
        if not teacher:
            return jsonify({
                "success": False,
                "message": "ไม่มีสิทธิ์ครู",
            })

        if not question_id or not answer_text:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกคำตอบ",
            })

        ws = get_worksheet("questions")
        headers = ensure_headers(ws, BASE_SHEETS["questions"])
        records = get_sheet_records("questions")

        rooms = get_teacher_rooms(teacher_line_user_id)

        for i, r in enumerate(records, start=2):
            if str(r.get("question_id", "")).strip() == question_id:
                classroom = normalize_classroom_text(r.get("classroom", ""))

                if classroom not in rooms:
                    return jsonify({
                        "success": False,
                        "message": "คุณไม่มีสิทธิ์ตอบคำถามของห้องนี้",
                    })

                answered_at = now_text()
                file = request.files.get("file")
                has_file = bool(file and file.filename)
                if has_file:
                    original_name = file.filename or "answer_file"
                    ext = file_ext_from_name(original_name)
                    if ext not in SUPPORTED_UPLOAD_EXTS:
                        return jsonify({
                            "success": False,
                            "message": "ไม่รองรับไฟล์ประเภทนี้",
                        })

                    file.seek(0, os.SEEK_END)
                    file_size = file.tell()
                    file.seek(0)
                    if file_size > app.config.get("MAX_CONTENT_LENGTH", 0):
                        max_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024)
                        return jsonify({
                            "success": False,
                            "message": f"ไฟล์ใหญ่เกิน {max_mb} MB กรุณาอัปโหลดขึ้น Google Drive แล้ววางลิงก์แทน",
                        }), 413

                    question_label = str(r.get("question_text", "")).strip()[:40] or question_id
                    safe_name = f"answer_{classroom}_{question_label}_{original_name}"
                    ext = file_ext_from_name(original_name)
                    if ext in {"heic", "heif"}:
                        file_bytes = file.read()
                        file_bytes, safe_name = convert_heic_to_jpeg(file_bytes, safe_name)
                        answer_attachment_url = upload_file_to_drive(
                            file_bytes=file_bytes,
                            file_name=safe_name,
                            classroom=classroom,
                            assignment_title="คำตอบคำถามนักเรียน",
                        )
                    else:
                        try:
                            file.stream.seek(0)
                        except Exception:
                            pass
                        answer_attachment_url = upload_file_to_drive(
                            file_stream=file.stream,
                            file_name=safe_name,
                            classroom=classroom,
                            assignment_title="คำตอบคำถามนักเรียน",
                        )
                    answer_attachment_name = original_name
                elif answer_attachment_url:
                    answer_attachment_name = "ลิงก์แนบคำตอบ"

                pin_target_rooms = []
                if pin_question:
                    pin_target_rooms = parse_pin_target_rooms(data, classroom)
                    pin_error = validate_pin_target_rooms(pin_target_rooms, rooms)
                    if pin_error:
                        return jsonify({
                            "success": False,
                            "message": pin_error,
                        })

                pinned_at = answered_at if pin_question else ""
                updates = [
                    {
                        "range": f"{col_letter(headers.index('status') + 1)}{i}",
                        "values": [["answered"]],
                    },
                    {
                        "range": f"{col_letter(headers.index('answer_text') + 1)}{i}",
                        "values": [[answer_text]],
                    },
                    {
                        "range": f"{col_letter(headers.index('answer_attachment_url') + 1)}{i}",
                        "values": [[answer_attachment_url]],
                    },
                    {
                        "range": f"{col_letter(headers.index('answer_attachment_name') + 1)}{i}",
                        "values": [[answer_attachment_name]],
                    },
                    {
                        "range": f"{col_letter(headers.index('answered_at') + 1)}{i}",
                        "values": [[answered_at]],
                    },
                    {
                        "range": f"{col_letter(headers.index('answered_by') + 1)}{i}",
                        "values": [[teacher_line_user_id]],
                    },
                    {
                        "range": f"{col_letter(headers.index('student_seen') + 1)}{i}",
                        "values": [["no"]],
                    },
                    {
                        "range": f"{col_letter(headers.index('is_pinned') + 1)}{i}",
                        "values": [["yes" if pin_question else "no"]],
                    },
                    {
                        "range": f"{col_letter(headers.index('pinned_at') + 1)}{i}",
                        "values": [[pinned_at]],
                    },
                    {
                        "range": f"{col_letter(headers.index('pinned_by') + 1)}{i}",
                        "values": [[teacher_line_user_id if pin_question else ""]],
                    },
                    {
                        "range": f"{col_letter(headers.index('pinned_classrooms') + 1)}{i}",
                        "values": [[" ".join(pin_target_rooms) if pin_question else ""]],
                    },
                ]

                ws.batch_update(updates, value_input_option="USER_ENTERED")
                invalidate_sheet_cache("questions")

                student_line_user_id = str(r.get("student_line_user_id", "")).strip()
                student_name = str(r.get("student_name", "")).strip()

                # ถ้านักเรียนแอดบอทไว้ จะ push ได้
                push_message(
                    student_line_user_id,
                    (
                        f"ครูตอบคำถามของคุณแล้ว\n\nคำตอบ:\n{answer_text}"
                        + (f"\n\nไฟล์แนบคำตอบ:\n{answer_attachment_url}" if answer_attachment_url else "")
                    ),
                )

                update_student_rich_menu(student_line_user_id)
                update_teacher_rich_menu(teacher_line_user_id)

                return jsonify({
                    "success": True,
                    "message": "ตอบคำถามเรียบร้อยแล้ว",
                })

        return jsonify({
            "success": False,
            "message": "ไม่พบคำถามนี้",
        })

    except Exception as e:
        print("[api_teacher_answer_question] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/announce", methods=["POST"])
def api_teacher_announce():
    try:
        data = request.get_json()

        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        message = str(data.get("message", "")).strip()

        teacher = get_teacher_by_line_user_id(teacher_line_user_id)
        if not teacher:
            return jsonify({
                "success": False,
                "message": "ไม่มีสิทธิ์ครู",
            })

        rooms = get_teacher_rooms(teacher_line_user_id)
        if classroom not in rooms:
            return jsonify({
                "success": False,
                "message": "คุณไม่ได้ดูแลห้องนี้",
            })

        if not classroom or not message:
            return jsonify({
                "success": False,
                "message": "กรุณาเลือกห้องและกรอกข้อความประกาศ",
            })

        teacher_name = first_record_value(teacher, ["teacher_name", "full_name", "name"])
        announcement_id = "an_" + uuid.uuid4().hex[:12]

        ws = get_worksheet("announcements")
        headers = ensure_headers(ws, BASE_SHEETS["announcements"])
        buffered_append_row(
            ws,
            row_values_for_headers(headers, {
                "announcement_id": announcement_id,
                "created_at": now_text(),
                "teacher_line_user_id": teacher_line_user_id,
                "line_user_id": teacher_line_user_id,
                "teacher_name": teacher_name,
                "full_name": teacher_name,
                "classroom": classroom,
                "message": message,
            }),
            value_input_option="RAW",
        )
        invalidate_sheet_cache("announcements")

        text = f"ประกาศห้อง {classroom}\n\n{message}"

        # ส่งเข้ากลุ่มถ้าผูกไว้
        group_id = get_class_group_id(classroom)
        if group_id:
            push_message(group_id, text)

        # ส่งส่วนตัวให้นักเรียนที่แอดบอทไว้
        students = get_students_by_classroom(classroom)
        for s in students:
            sid = str(s.get("student_line_user_id", "")).strip()
            if sid:
                push_message(sid, text)

        return jsonify({
            "success": True,
            "message": "ส่งประกาศเรียบร้อยแล้ว",
        })

    except Exception as e:
        print("[api_teacher_announce] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/update-assignment-weight", methods=["POST"])
def api_teacher_update_assignment_weight():
    try:
        data = request.get_json()
        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        assignment_id = str(data.get("assignment_id", "")).strip()
        score_weight = str(data.get("score_weight", "")).strip()

        teacher = get_teacher_by_line_user_id(teacher_line_user_id)
        if not teacher:
            return jsonify({"success": False, "message": "ไม่มีสิทธิ์ครู"})

        rooms = get_teacher_rooms(teacher_line_user_id)
        if classroom not in rooms:
            return jsonify({"success": False, "message": "คุณไม่ได้ดูแลห้องนี้"})

        assignment = get_assignment_by_id(assignment_id)
        if not assignment:
            return jsonify({"success": False, "message": "ไม่พบงานที่เลือก"})

        if normalize_classroom_text(assignment.get("classroom", "")) != classroom:
            return jsonify({"success": False, "message": "งานนี้ไม่ได้อยู่ในห้องที่เลือก"})

        score_weight_value = parse_assignment_weight(score_weight)
        if score_weight_value is None:
            return jsonify({"success": False, "message": "น้ำหนักคะแนนต้องเป็นตัวเลข"})

        if score_weight_value < 0:
            return jsonify({"success": False, "message": "น้ำหนักคะแนนต้องไม่ติดลบ"})

        if score_weight_value > 60:
            return jsonify({"success": False, "message": "น้ำหนักคะแนนต้องไม่เกิน 60"})

        row_i, _ = find_assignment_row(assignment_id)
        if not row_i:
            return jsonify({"success": False, "message": "ไม่พบงานในแผ่นงาน"})

        ws = get_worksheet("assignments")
        headers = get_sheet_headers("assignments")
        if "score_weight" not in headers:
            return jsonify({"success": False, "message": "ไม่พบคอลัมน์น้ำหนักคะแนน"})

        col_i = headers.index("score_weight")
        ws.update_cell(row_i, col_i + 1, format_score_value(score_weight_value) if score_weight_value else "0")
        invalidate_sheet_cache("assignments")

        return jsonify({
            "success": True,
            "message": "อัปเดตน้ำหนักคะแนนเรียบร้อยแล้ว",
        })

    except Exception as e:
        print("[api_teacher_update_assignment_weight] Error:", e)
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/cron/notify-deadlines", methods=["POST", "GET"])
def api_cron_notify_deadlines():
    """
    สแกนงานทั้งหมด และส่งแจ้งเตือนเมื่อถึงกำหนดส่งสำหรับทุกงานและทุกห้อง
    บันทึกลงในชีต `deadline_logs` เพื่อลดการแจ้งซ้ำ
    """
    try:
        now = now_dt()
        assignments = get_sheet_records("assignments")

        created = 0
        for assignment in assignments:
            assignment = add_assignment_due_metadata(dict(assignment))
            assignment_id = str(assignment.get("assignment_id", "")).strip()
            classroom = normalize_classroom_text(assignment.get("classroom", ""))
            if not assignment_id or not classroom:
                continue

            due_dt = assignment_due_datetime(assignment)
            if not due_dt:
                continue

            # ถ้ายังไม่ถึงกำหนด ไม่ส่ง
            if now.replace(tzinfo=None) < due_dt:
                continue

            # ตรวจสอบว่ามีการบันทึกแจ้งเตือนนี้แล้วหรือไม่ (สำหรับ assignment_id + classroom)
            if deadline_log_exists(
                assignment_id,
                classroom,
                DEADLINE_LOG_TYPE_NOTICE,
                count_legacy=True,
            ):
                continue

            # สร้างข้อความแจ้งเตือน
            title = str(assignment.get("title", "")).strip() or assignment_id
            due_text = assignment.get("due_text", assignment_due_text(assignment))
            text = f"ถึงกำหนดส่ง: {title}\nห้อง: {classroom}\nกำหนดส่ง: {due_text}\nกรุณาส่งงานทันเวลา"

            # ส่งเข้ากลุ่มถ้าผูกไว้
            group_id = get_class_group_id(classroom)
            if group_id:
                push_message(group_id, text)

            # ส่งส่วนตัวให้นักเรียนที่แอดบอทไว้
            for s in get_students_by_classroom(classroom):
                sid = str(s.get("student_line_user_id", "")).strip()
                if sid:
                    push_message(sid, text)

            # บันทึกลง deadline_logs
            append_deadline_log(
                assignment_id,
                classroom,
                group_id,
                text,
                DEADLINE_LOG_TYPE_NOTICE,
            )
            created += 1

        return jsonify({
            "success": True,
            "message": f"ส่งแจ้งเตือนกำหนดส่งเสร็จ {created} งาน",
            "created": created,
        })

    except Exception as e:
        print("[api_cron_notify_deadlines] Error:", e)
        return jsonify({"success": False, "message": str(e)})


ATTENDANCE_STATUSES = {"มา", "กิจกรรม", "สาย", "ลา", "ขาด", "หนี"}


def attendance_record_key(record):
    return (
        str(record.get("attendance_date", "")).strip(),
        str(record.get("start_time", "")).strip(),
        str(record.get("end_time", "")).strip(),
        normalize_classroom_text(record.get("classroom", "")),
        str(record.get("student_line_user_id", "")).strip(),
    )


@app.route("/api/teacher/attendance", methods=["GET"])
def api_teacher_attendance_get():
    try:
        teacher_line_user_id = str(
            request.args.get("teacher_line_user_id", "") or request.args.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))
        attendance_date = str(
            request.args.get("attendance_date", "") or request.args.get("date", "")
        ).strip()
        start_time = str(request.args.get("start_time", "")).strip()
        end_time = str(request.args.get("end_time", "")).strip()

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        if not attendance_date or not start_time or not end_time:
            return jsonify({
                "success": False,
                "message": "กรุณาเลือกวันที่และเวลาเรียน",
            })

        ws = get_worksheet("attendance")
        ensure_headers(ws, BASE_SHEETS["attendance"])

        students = get_students_by_classroom(classroom)
        records = get_sheet_records("attendance")
        existing_by_student = {
            str(r.get("student_line_user_id", "")).strip(): r
            for r in records
            if attendance_record_key(r)[:4] == (attendance_date, start_time, end_time, classroom)
        }

        result_students = []
        for student in students:
            sid = str(student.get("student_line_user_id", "")).strip()
            existing = existing_by_student.get(sid, {})
            result_students.append({
                "student_line_user_id": sid,
                "student_name": str(student.get("student_name", "")).strip(),
                "student_code": str(student.get("student_code", "")).strip(),
                "classroom": classroom,
                "status": str(existing.get("status", "")).strip() or "มา",
                "note": str(existing.get("note", "")).strip(),
            })

        return jsonify({
            "success": True,
            "students": result_students,
        })

    except Exception as e:
        print("[api_teacher_attendance_get] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/attendance", methods=["POST"])
def api_teacher_attendance_post():
    try:
        data = request.get_json() or {}
        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        attendance_date = str(data.get("attendance_date", "") or data.get("date", "")).strip()
        start_time = str(data.get("start_time", "")).strip()
        end_time = str(data.get("end_time", "")).strip()
        students = data.get("students") or []

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        if not attendance_date or not start_time or not end_time:
            return jsonify({
                "success": False,
                "message": "กรุณาเลือกวันที่และเวลาเรียน",
            })

        if not isinstance(students, list) or not students:
            return jsonify({
                "success": False,
                "message": "ยังไม่มีรายชื่อนักเรียนสำหรับบันทึก",
            })

        ws = get_worksheet("attendance")
        headers = ensure_headers(ws, BASE_SHEETS["attendance"])
        records = get_sheet_records("attendance")
        existing_rows = {
            attendance_record_key(record): row
            for row, record in enumerate(records, start=2)
        }

        updates = []
        updated_rows = set()
        appended = 0
        checked_at = now_text()

        for student in students:
            sid = str(student.get("student_line_user_id", "")).strip()
            if not sid:
                continue

            status = str(student.get("status", "")).strip() or "มา"
            if status not in ATTENDANCE_STATUSES:
                status = "มา"

            values = {
                "attendance_id": "att_" + uuid.uuid4().hex[:12],
                "attendance_date": attendance_date,
                "start_time": start_time,
                "end_time": end_time,
                "classroom": classroom,
                "student_line_user_id": sid,
                "student_name": str(student.get("student_name", "")).strip(),
                "student_code": str(student.get("student_code", "")).strip(),
                "status": status,
                "note": str(student.get("note", "")).strip(),
                "checked_at": checked_at,
                "checked_by": teacher_line_user_id,
            }
            key = (
                attendance_date,
                start_time,
                end_time,
                classroom,
                sid,
            )
            row = existing_rows.get(key)

            if row:
                updated_rows.add(row)
                for header in [
                    "student_name",
                    "student_code",
                    "status",
                    "note",
                    "checked_at",
                    "checked_by",
                ]:
                    if header in headers:
                        updates.append({
                            "range": f"{col_letter(headers.index(header) + 1)}{row}",
                            "values": [[values[header]]],
                        })
            else:
                buffered_append_row(
                    ws,
                    row_values_for_headers(headers, values),
                    value_input_option="RAW",
                )
                appended += 1

        if updates:
            ws.batch_update(updates, value_input_option="RAW")

        invalidate_sheet_cache("attendance")

        try:
            create_or_update_attendance_sheet(classroom)
        except Exception as e:
            print("[api_teacher_attendance_post sheet] Error:", e)

        return jsonify({
            "success": True,
            "message": "บันทึกเช็คชื่อเรียบร้อยแล้ว",
            "count": len([s for s in students if str(s.get("student_line_user_id", "")).strip()]),
            "appended": appended,
            "updated": len(updated_rows),
        })

    except Exception as e:
        print("[api_teacher_attendance_post] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/exam-scores", methods=["GET"])
def api_teacher_exam_scores_get():
    try:
        teacher_line_user_id = str(
            request.args.get("teacher_line_user_id", "") or request.args.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        ws = get_worksheet("exam_scores")
        ensure_headers(ws, BASE_SHEETS["exam_scores"])

        exam_scores = get_exam_scores_by_classroom(classroom)
        students = []
        for student in get_students_by_classroom(classroom):
            sid = str(student.get("student_line_user_id", "")).strip()
            existing = exam_scores.get(sid, {})
            students.append({
                "student_line_user_id": sid,
                "student_name": str(student.get("student_name", "")).strip(),
                "student_code": str(student.get("student_code", "")).strip(),
                "midterm_score": str(existing.get("midterm_score", "")).strip(),
                "final_score": str(existing.get("final_score", "")).strip(),
            })

        return jsonify({
            "success": True,
            "students": students,
        })

    except Exception as e:
        print("[api_teacher_exam_scores_get] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/exam-scores", methods=["POST"])
def api_teacher_exam_scores_post():
    try:
        data = request.get_json() or {}
        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        students = data.get("students") or []

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        if not isinstance(students, list) or not students:
            return jsonify({
                "success": False,
                "message": "ยังไม่มีรายชื่อนักเรียนสำหรับบันทึกคะแนน",
            })

        ws = get_worksheet("exam_scores")
        headers = ensure_headers(ws, BASE_SHEETS["exam_scores"])
        records = get_sheet_records("exam_scores")
        existing_rows = {
            (
                str(record.get("classroom", "")).strip(),
                str(record.get("student_line_user_id", "")).strip(),
            ): row
            for row, record in enumerate(records, start=2)
        }

        updates = []
        updated_rows = set()
        appended = 0
        updated_at = now_text()

        for student in students:
            sid = str(student.get("student_line_user_id", "")).strip()
            if not sid:
                continue

            midterm_text = str(student.get("midterm_score", "")).strip()
            final_text = str(student.get("final_score", "")).strip()
            midterm = parse_float_value(midterm_text, 0) if midterm_text else ""
            final = parse_float_value(final_text, 0) if final_text else ""

            if midterm != "" and (midterm < 0 or midterm > 20):
                return jsonify({
                    "success": False,
                    "message": "คะแนนกลางภาคต้องอยู่ระหว่าง 0-20",
                })

            if final != "" and (final < 0 or final > 20):
                return jsonify({
                    "success": False,
                    "message": "คะแนนปลายภาคต้องอยู่ระหว่าง 0-20",
                })

            values = {
                "exam_score_id": "ex_" + uuid.uuid4().hex[:12],
                "classroom": classroom,
                "student_line_user_id": sid,
                "student_name": str(student.get("student_name", "")).strip(),
                "student_code": str(student.get("student_code", "")).strip(),
                "midterm_score": format_score_value(midterm) if midterm != "" else "",
                "final_score": format_score_value(final) if final != "" else "",
                "updated_at": updated_at,
                "updated_by": teacher_line_user_id,
            }

            row = existing_rows.get((classroom, sid))
            if row:
                updated_rows.add(row)
                for header in [
                    "student_name",
                    "student_code",
                    "midterm_score",
                    "final_score",
                    "updated_at",
                    "updated_by",
                ]:
                    if header in headers:
                        updates.append({
                            "range": f"{col_letter(headers.index(header) + 1)}{row}",
                            "values": [[values[header]]],
                        })
            else:
                buffered_append_row(
                    ws,
                    row_values_for_headers(headers, values),
                    value_input_option="RAW",
                )
                appended += 1

        if updates:
            ws.batch_update(updates, value_input_option="RAW")

        invalidate_sheet_cache("exam_scores")
        mark_room_dirty(classroom, "exam_scores_updated")

        return jsonify({
            "success": True,
            "message": "บันทึกคะแนนสอบเรียบร้อยแล้ว",
            "count": len([s for s in students if str(s.get("student_line_user_id", "")).strip()]),
            "appended": appended,
            "updated": len(updated_rows),
        })

    except Exception as e:
        print("[api_teacher_exam_scores_post] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })



@app.route("/api/teacher/score-visibility", methods=["GET"])
def api_teacher_score_visibility_get():
    try:
        teacher_line_user_id = str(
            request.args.get("teacher_line_user_id", "") or request.args.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        ws = get_worksheet("score_visibility")
        ensure_headers(ws, BASE_SHEETS["score_visibility"])
        visibility = get_score_visibility_by_classroom(classroom)
        return jsonify({
            "success": True,
            "visibility": visibility,
            "assignments": get_assignment_visibility_payload(classroom),
            "exam_visibility": {
                "show_midterm_scores": visibility.get("show_midterm_scores", False),
                "show_final_scores": visibility.get("show_final_scores", False),
            },
        })

    except Exception as e:
        print("[api_teacher_score_visibility_get] Error:", e)
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/teacher/score-visibility", methods=["POST"])
def api_teacher_score_visibility_post():
    try:
        data = request.get_json() or {}
        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))
        assignment_items = data.get("assignments") or []
        show_midterm_scores = parse_bool(data.get("show_midterm_scores", False), default=False)
        show_final_scores = parse_bool(data.get("show_final_scores", False), default=False)

        if "show_exam_scores" in data and "show_midterm_scores" not in data:
            legacy_exam = parse_bool(data.get("show_exam_scores", False), default=False)
            show_midterm_scores = legacy_exam
            show_final_scores = legacy_exam

        _, access_error = validate_teacher_classroom_access(
            teacher_line_user_id,
            classroom,
            "กรุณาเลือกห้อง",
        )
        if access_error:
            return jsonify({"success": False, "message": access_error})

        classroom_assignments = {
            str(a.get("assignment_id", "")).strip(): a
            for a in get_assignments_by_classroom(classroom)
            if str(a.get("assignment_id", "")).strip()
        }

        assignment_ws = get_worksheet("assignments")
        assignment_headers = ensure_headers(assignment_ws, BASE_SHEETS["assignments"])
        assignment_updates = []

        for item in assignment_items:
            assignment_id = str(item.get("assignment_id", "")).strip()
            if not assignment_id or assignment_id not in classroom_assignments:
                continue

            row_i, _ = find_assignment_row(assignment_id)
            if not row_i:
                continue

            show_score = parse_bool(item.get("show_score_to_students", False), default=False)
            if "show_score_to_students" in assignment_headers:
                assignment_updates.append({
                    "range": f"{col_letter(assignment_headers.index('show_score_to_students') + 1)}{row_i}",
                    "values": [[bool_to_sheet_text(show_score)]],
                })

        if assignment_updates:
            assignment_ws.batch_update(assignment_updates, value_input_option="RAW")
            invalidate_sheet_cache("assignments")

        visibility_ws = get_worksheet("score_visibility")
        visibility_headers = ensure_headers(visibility_ws, BASE_SHEETS["score_visibility"])
        visibility_records = get_sheet_records("score_visibility")
        updated_at = now_text()
        values = {
            "classroom": classroom,
            "show_midterm_scores": bool_to_sheet_text(show_midterm_scores),
            "show_final_scores": bool_to_sheet_text(show_final_scores),
            "show_work_scores": "no",
            "show_exam_scores": bool_to_sheet_text(show_midterm_scores or show_final_scores),
            "updated_at": updated_at,
            "updated_by": teacher_line_user_id,
        }

        target_row = None
        for i, r in enumerate(visibility_records, start=2):
            if normalize_classroom_text(r.get("classroom", "")) == classroom:
                target_row = i
                break

        if target_row:
            updates = []
            for header in [
                "show_midterm_scores",
                "show_final_scores",
                "show_work_scores",
                "show_exam_scores",
                "updated_at",
                "updated_by",
            ]:
                if header in visibility_headers:
                    updates.append({
                        "range": f"{col_letter(visibility_headers.index(header) + 1)}{target_row}",
                        "values": [[values[header]]],
                    })
            if updates:
                visibility_ws.batch_update(updates, value_input_option="RAW")
        else:
            buffered_append_row(
                visibility_ws,
                row_values_for_headers(visibility_headers, values),
                value_input_option="RAW",
            )

        invalidate_sheet_cache("score_visibility")
        visibility = get_score_visibility_by_classroom(classroom)

        return jsonify({
            "success": True,
            "message": "บันทึกการแสดงคะแนนเรียบร้อยแล้ว",
            "visibility": visibility,
            "assignments": get_assignment_visibility_payload(classroom),
            "exam_visibility": {
                "show_midterm_scores": visibility.get("show_midterm_scores", False),
                "show_final_scores": visibility.get("show_final_scores", False),
            },
        })

    except Exception as e:
        print("[api_teacher_score_visibility_post] Error:", e)
        return jsonify({"success": False, "message": str(e)})

# =========================================================
# API: Student
# =========================================================

def get_students_by_classroom(classroom):
    records = get_sheet_records("students")

    return sort_students_by_code([
        r for r in records
        if str(r.get("classroom", "")).strip() == str(classroom).strip()
    ])


def write_registration_to_sheets(student_line_user_id, student_name, student_code, classroom, created_at=None):
    register_lock = get_operation_lock(("student_register", student_line_user_id))
    with register_lock:
        invalidate_sheet_cache("students")
        ws = get_worksheet("students")
        ensure_headers(ws, BASE_SHEETS["students"])
        records = get_sheet_records("students")
        headers = get_sheet_headers("students")

        existing_line_row = None
        matching_code_row = None

        for i, r in enumerate(records, start=2):
            rec_line_id = str(r.get("student_line_user_id", "")).strip()
            rec_name = str(r.get("student_name", "")).strip()
            rec_code = str(r.get("student_code", "")).strip()
            rec_classroom = normalize_classroom_text(r.get("classroom", ""))

            if rec_line_id == student_line_user_id:
                existing_line_row = (i, r)

            if rec_classroom == classroom and rec_code == student_code:
                if rec_name != student_name:
                    raise ValueError(
                        f"เลขที่ {student_code} ในห้อง {classroom} ถูกใช้โดย {rec_name} แล้ว"
                    )
                matching_code_row = (i, r)

        if matching_code_row is not None:
            match_index, _ = matching_code_row
            if existing_line_row is not None and existing_line_row[0] != match_index:
                ws.delete_rows(existing_line_row[0])
                if existing_line_row[0] < match_index:
                    match_index -= 1

            call_google_sheet_api(
                lambda: ws.batch_update([
                    {
                        "range": f"{col_letter(headers.index('student_line_user_id') + 1)}{match_index}",
                        "values": [[student_line_user_id]],
                    },
                    {
                        "range": f"{col_letter(headers.index('student_name') + 1)}{match_index}",
                        "values": [[student_name]],
                    },
                    {
                        "range": f"{col_letter(headers.index('student_code') + 1)}{match_index}",
                        "values": [[student_code]],
                    },
                    {
                        "range": f"{col_letter(headers.index('classroom') + 1)}{match_index}",
                        "values": [[classroom]],
                    },
                ], value_input_option="USER_ENTERED")
            )
            invalidate_sheet_cache("students")
            add_student_to_classroom_sheet(
                student_code,
                student_name,
                student_line_user_id,
                classroom,
            )
            return "updated"

        if existing_line_row is not None:
            row_index, _ = existing_line_row
            call_google_sheet_api(
                lambda: ws.batch_update([
                    {
                        "range": f"{col_letter(headers.index('student_name') + 1)}{row_index}",
                        "values": [[student_name]],
                    },
                    {
                        "range": f"{col_letter(headers.index('student_code') + 1)}{row_index}",
                        "values": [[student_code]],
                    },
                    {
                        "range": f"{col_letter(headers.index('classroom') + 1)}{row_index}",
                        "values": [[classroom]],
                    },
                ], value_input_option="USER_ENTERED")
            )
            invalidate_sheet_cache("students")
            add_student_to_classroom_sheet(
                student_code,
                student_name,
                student_line_user_id,
                classroom,
            )
            return "updated"

        created_at_value = None
        if created_at is not None:
            if hasattr(created_at, 'strftime'):
                created_at_value = created_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                created_at_value = str(created_at).strip()
        if not created_at_value:
            created_at_value = now_text()

        buffered_append_row(ws, [
            student_line_user_id,
            student_name,
            student_code,
            classroom,
            created_at_value,
        ], value_input_option="USER_ENTERED")
        invalidate_sheet_cache("students")
        add_student_to_classroom_sheet(
            student_code,
            student_name,
            student_line_user_id,
            classroom,
        )
        return "created"


def write_submission_to_sheets(data):
    student_line_user_id = str(data.get("student_line_user_id", "")).strip()
    assignment_id = str(data.get("assignment_id", "")).strip()
    if not student_line_user_id or not assignment_id:
        raise ValueError("missing student_line_user_id or assignment_id")

    submission_lock = get_operation_lock(("student_submit", student_line_user_id, assignment_id))
    with submission_lock:
        invalidate_sheet_cache("submissions")
        current_submission_row, current_submission = find_submission_row(student_line_user_id, assignment_id)
        ws = get_worksheet("submissions")
        headers = ensure_headers(ws, BASE_SHEETS["submissions"])

        values = {
            "submitted_at": str(data.get("submitted_at", "")).strip() or now_text(),
            "assignment_id": assignment_id,
            "assignment_title": str(data.get("assignment_title", "")).strip(),
            "student_line_user_id": student_line_user_id,
            "student_name": str(data.get("student_name", "")).strip(),
            "student_code": str(data.get("student_code", "")).strip(),
            "classroom": str(data.get("classroom", "")).strip(),
            "file_url": str(data.get("file_url", "")).strip(),
            "file_name": str(data.get("file_name", "")).strip(),
            "note": str(data.get("note", "")).strip(),
            "late": str(data.get("late", "")).strip(),
            "auto_score": str(data.get("auto_score", "")).strip(),
            "score": "",
            "checked_at": "",
            "checked_by": "",
            "teacher_comment": "",
        }

        if current_submission:
            updates = []
            for header, value in values.items():
                if header == "assignment_id":
                    continue
                if header in headers:
                    updates.append({
                        "range": f"{col_letter(headers.index(header) + 1)}{current_submission_row}",
                        "values": [[value]],
                    })
            if updates:
                call_google_sheet_api(
                    lambda: ws.batch_update(updates, value_input_option="USER_ENTERED")
                )
            action = "updated"
        else:
            values["submission_id"] = "sub_" + uuid.uuid4().hex[:12]
            buffered_append_row(
                ws,
                row_values_for_headers(headers, values),
                value_input_option="USER_ENTERED",
            )
            action = "created"

        invalidate_sheet_cache("submissions")
        mark_submission_in_classroom_sheet(
            values["classroom"],
            student_line_user_id,
            values["assignment_title"],
            values["file_url"],
        )
        return action


def sync_db_to_sheets(limit=50):
    if not db_enabled():
        return {
            "success": False,
            "message": "DATABASE_URL is not configured",
            "registrations": {"synced": 0, "failed": 0},
            "submissions": {"synced": 0, "failed": 0},
        }

    result = {
        "success": True,
        "registrations": {"synced": 0, "failed": 0},
        "submissions": {"synced": 0, "failed": 0},
    }

    for item in fetch_pending_db_rows("student_registrations", limit):
        try:
            write_registration_to_sheets(
                str(item.get("student_line_user_id", "")).strip(),
                str(item.get("student_name", "")).strip(),
                str(item.get("student_code", "")).strip(),
                normalize_classroom_text(item.get("classroom", "")),
                item.get("created_at"),
            )
            mark_db_sheets_status(
                "student_registrations",
                {"student_line_user_id": item.get("student_line_user_id", "")},
                "synced",
            )
            result["registrations"]["synced"] += 1
        except Exception as e:
            mark_db_sheets_status(
                "student_registrations",
                {"student_line_user_id": item.get("student_line_user_id", "")},
                "sheet_failed",
                e,
            )
            result["registrations"]["failed"] += 1

    for item in fetch_pending_db_rows("student_submissions", limit):
        try:
            write_submission_to_sheets(item)
            mark_db_sheets_status(
                "student_submissions",
                {
                    "student_line_user_id": item.get("student_line_user_id", ""),
                    "assignment_id": item.get("assignment_id", ""),
                },
                "synced",
            )
            result["submissions"]["synced"] += 1
        except Exception as e:
            mark_db_sheets_status(
                "student_submissions",
                {
                    "student_line_user_id": item.get("student_line_user_id", ""),
                    "assignment_id": item.get("assignment_id", ""),
                },
                "sheet_failed",
                e,
            )
            result["submissions"]["failed"] += 1

    return result


@app.route("/api/student/register", methods=["POST"])
def api_student_register():
    try:
        data = request.get_json()

        student_line_user_id = str(
            data.get("student_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        student_name = str(
            data.get("student_name", "") or data.get("full_name", "")
        ).strip()
        student_code = str(data.get("student_code", "")).strip()
        classroom = normalize_classroom_text(data.get("classroom", ""))

        if not student_line_user_id or not student_name or not student_code or not classroom:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกข้อมูลให้ครบ",
            })

        invalidate_sheet_cache("students")
        records = get_sheet_records("students")
        for r in records:
            rec_classroom = normalize_classroom_text(r.get("classroom", ""))
            rec_code = str(r.get("student_code", "")).strip()
            rec_name = str(r.get("student_name", "")).strip()
            if rec_classroom == classroom and rec_code == student_code and rec_name != student_name:
                return jsonify({
                    "success": False,
                    "message": f"เลขที่ {student_code} ในห้อง {classroom} ถูกใช้โดย {rec_name} แล้ว",
                })

        db_registration_saved = save_registration_to_db(
            student_line_user_id,
            student_name,
            student_code,
            classroom,
        )

        if db_registration_saved:
            link_student_default_rich_menu(student_line_user_id)
            return jsonify({
                "success": True,
                "message": "ลงทะเบียนเรียบร้อยแล้ว ระบบรับข้อมูลแล้ว",
                "queued": True,
            })

        action = write_registration_to_sheets(
            student_line_user_id,
            student_name,
            student_code,
            classroom,
        )
        link_student_default_rich_menu(student_line_user_id)

        return jsonify({
            "success": True,
            "message": "อัปเดตข้อมูลนักเรียนเรียบร้อยแล้ว" if action == "updated" else "ลงทะเบียนเรียบร้อยแล้ว",
            "queued": False,
        })

    except Exception as e:
        print("[api_student_register] Error:", e)
        if locals().get("db_registration_saved"):
            mark_db_sheets_status(
                "student_registrations",
                {"student_line_user_id": locals().get("student_line_user_id", "")},
                "sheet_failed",
                e,
            )
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/student/my-info")
def api_student_my_info():
    student_line_user_id = request.args.get("student_line_user_id", "").strip()

    student = get_student_by_line_user_id(student_line_user_id)
    if not student:
        return jsonify({
            "success": False,
            "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
        })

    return jsonify({
        "success": True,
        "student": student,
    })


@app.route("/api/student/assignments")
def api_student_assignments():
    student_line_user_id = request.args.get("student_line_user_id", "").strip()

    student = get_student_by_line_user_id(student_line_user_id)
    if not student:
        return jsonify({
            "success": False,
            "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
        })

    classroom = str(student.get("classroom", "")).strip()
    assignments = get_assignments_by_classroom(classroom)
    submissions = get_submissions_by_student(student_line_user_id)

    submitted_ids = set(str(s.get("assignment_id", "")).strip() for s in submissions)
    submission_by_assignment = {
        str(s.get("assignment_id", "")).strip(): s
        for s in submissions
        if str(s.get("assignment_id", "")).strip()
    }

    result = []
    for a in assignments:
        aid = str(a.get("assignment_id", "")).strip()
        item = student_assignment_payload(a)
        add_assignment_file_type_metadata(item)
        item["submitted"] = aid in submitted_ids
        show_score = assignment_show_score_for_classroom(a, classroom)
        item["show_score_to_students"] = show_score
        item["counts_in_coursework"] = assignment_counts_in_coursework(a)
        sub = submission_by_assignment.get(aid)
        item["can_edit_submission"] = bool(sub and submission_edit_allowed(item))
        item["edit_deadline_text"] = assignment_due_text(item)
        if sub:
            submission_payload = {
                "submitted_at": str(sub.get("submitted_at", "")).strip(),
                "late": str(sub.get("late", "")).strip(),
                "auto_score": str(sub.get("auto_score", "")).strip(),
                "score": str(sub.get("score", "")).strip(),
                "teacher_comment": str(sub.get("teacher_comment", "")).strip(),
                "checked_at": str(sub.get("checked_at", "")).strip(),
                "file_url": str(sub.get("file_url", "")).strip(),
                "can_edit": item["can_edit_submission"],
            }
            item["submission"] = submission_payload
        else:
            item["submission"] = None
        result.append(item)

    return jsonify({
        "success": True,
        "student": student,
        "assignments": result,
    })


@app.route("/api/student/score-attendance")
def api_student_score_attendance():
    try:
        student_line_user_id = request.args.get("student_line_user_id", "").strip()
        student = get_student_by_line_user_id(student_line_user_id)
        if not student:
            return jsonify({
                "success": False,
                "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
            })

        classroom = normalize_classroom_text(student.get("classroom", ""))
        attendance_summary = attendance_summary_for_student(classroom, student_line_user_id)
        score_summary = student_score_summary_for_announcement(student)

        return jsonify({
            "success": True,
            "student": student,
            "attendance": attendance_summary,
            "score_summary": score_summary,
        })

    except Exception as e:
        print("[api_student_score_attendance] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })

@app.route("/api/student/scores")
def api_student_scores():
    try:
        student_line_user_id = request.args.get("student_line_user_id", "").strip()

        student = get_student_by_line_user_id(student_line_user_id)
        if not student:
            return jsonify({
                "success": False,
                "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
            })

        classroom = normalize_classroom_text(student.get("classroom", ""))
        visibility = get_score_visibility_by_classroom(classroom)

        # ดึงข้อมูลคะแนนของนักเรียน
        assignments = get_assignments_by_classroom(classroom)
        submissions = get_submissions_by_student(student_line_user_id)
        submission_dict = {str(s.get("assignment_id", "")).strip(): s for s in submissions}

        scores = []
        for a in assignments:
            a = add_assignment_due_metadata(dict(a))
            assignment_id = str(a.get("assignment_id", "")).strip()
            submission = submission_dict.get(assignment_id)

            # ตรวจสอบว่า visibility เปิดให้ดูคะแนนหรือไม่
            score_category = normalize_score_category(a.get("score_category", ""))
            show_score = False

            if score_category == "quiz":
                show_score = parse_bool(visibility.get("show_midterm_scores", False), default=False) or parse_bool(visibility.get("show_final_scores", False), default=False)
            elif score_category in ["assignment", "notebook"]:
                show_score = parse_bool(visibility.get("show_work_scores", False), default=False)

            if submission or show_score:
                scores.append({
                    "assignment_id": assignment_id,
                    "title": a.get("title", ""),
                    "chapter_name": a.get("chapter_name", ""),
                    "score_category": score_category,
                    "score_category_label": a.get("score_category_label", ""),
                    "max_score": a.get("max_score", ""),
                    "score": submission.get("score", "") if submission else "",
                    "teacher_comment": submission.get("teacher_comment", "") if submission else "",
                    "submission_status": get_submission_status(submission, a) if submission else "ยังไม่ส่ง",
                    "submitted_at": submission.get("submitted_at", "") if submission else "",
                })

        # คำนวณสรุปคะแนน
        summary = calculate_score_summary(student, classroom) if visibility.get("show_work_scores") or visibility.get("show_midterm_scores") or visibility.get("show_final_scores") else None

        return jsonify({
            "success": True,
            "student_line_user_id": student_line_user_id,
            "student_name": student.get("student_name", ""),
            "student_code": student.get("student_code", ""),
            "classroom": classroom,
            "scores": scores,
            "summary": summary,
        })

    except Exception as e:
        print("[api_student_scores] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/student/attendance")
def api_student_attendance():
    try:
        student_line_user_id = request.args.get("student_line_user_id", "").strip()
        classroom = normalize_classroom_text(request.args.get("classroom", ""))

        student = get_student_by_line_user_id(student_line_user_id)
        if not student:
            return jsonify({
                "success": False,
                "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
            })

        classroom = normalize_classroom_text(student.get("classroom", ""))

        # ดึงข้อมูลการมาเรียน
        attendance_records = get_sheet_records("attendance")
        student_attendance = [
            r for r in attendance_records
            if str(r.get("student_line_user_id", "")).strip() == student_line_user_id
            and normalize_classroom_text(r.get("classroom", "")) == classroom
        ]

        # จัดเรียงตามวัน
        student_attendance.sort(key=lambda r: r.get("attendance_date", ""), reverse=True)

        attendance = []
        for att in student_attendance:
            status = str(att.get("status", "")).strip()
            status_class = "present"
            if status in ["ขาด", "absent"]:
                status_class = "absent"
            elif status in ["สาย", "late"]:
                status_class = "late"
            elif status in ["ลา", "leave"]:
                status_class = "leave"

            attendance.append({
                "date": att.get("attendance_date", ""),
                "status": status,
                "status_class": status_class,
                "note": att.get("note", ""),
            })

        return jsonify({
            "success": True,
            "student_line_user_id": student_line_user_id,
            "classroom": classroom,
            "attendance": attendance,
        })

    except Exception as e:
        print("[api_student_attendance] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


def get_submission_status(submission, assignment):
    if not submission:
        return "ยังไม่ส่ง"

    late = str(submission.get("late", "")).strip().lower()
    if late in ["ใช่", "yes", "true", "1", "late", "เลท", "เลยกำหนด"]:
        return "ส่งแล้ว (เลท)"

    return "ส่งแล้ว (ตรงเวลา)"


def calculate_score_summary(student, classroom):
    try:
        assignments = get_assignments_by_classroom(classroom)
        submissions = get_submissions_by_student(student.get("student_line_user_id", ""))
        submission_dict = {str(s.get("assignment_id", "")).strip(): s for s in submissions}

        summary = {}
        for a in assignments:
            a = add_assignment_due_metadata(dict(a))
            category = normalize_score_category(a.get("score_category", ""))
            category_label = a.get("score_category_label", "")

            assignment_id = str(a.get("assignment_id", "")).strip()
            submission = submission_dict.get(assignment_id)

            if submission and submission.get("score"):
                score = float(submission.get("score", 0))
                if category not in summary:
                    summary[category] = {"total": 0, "count": 0, "label": category_label}
                summary[category]["total"] += score
                summary[category]["count"] += 1

        # คำนวณค่าเฉลี่ย
        result = {}
        for category, data in summary.items():
            avg = data["total"] / data["count"] if data["count"] > 0 else 0
            result[category] = f"{avg:.2f}" if avg > 0 else "ไม่มี"

        return result
    except Exception as e:
        print("[calculate_score_summary] Error:", e)
        return None


@app.route("/api/student/pending")
def api_student_pending():
    student_line_user_id = request.args.get("student_line_user_id", "").strip()

    student = get_student_by_line_user_id(student_line_user_id)
    if not student:
        return jsonify({
            "success": False,
            "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
        })

    classroom = str(student.get("classroom", "")).strip()
    assignments = get_assignments_by_classroom(classroom)
    submissions = get_submissions_by_student(student_line_user_id)
    submitted_ids = set(str(s.get("assignment_id", "")).strip() for s in submissions)

    pending = []
    submitted = []

    for a in assignments:
        aid = str(a.get("assignment_id", "")).strip()
        if not assignment_requires_submission(a):
            continue
        if aid in submitted_ids:
            submitted.append(student_assignment_payload(a))
        else:
            pending.append(student_assignment_payload(a))

    update_student_rich_menu(student_line_user_id)

    return jsonify({
        "success": True,
        "student": student,
        "pending": pending,
        "submitted": submitted,
    })


@app.route("/api/student/submit", methods=["POST"])
def api_student_submit():
    try:
        student_line_user_id = (
            request.form.get("student_line_user_id", "")
            or request.form.get("line_user_id", "")
        ).strip()
        assignment_id = request.form.get("assignment_id", "").strip()
        note = request.form.get("note", "").strip()
        submission_url = (
            request.form.get("submission_url", "")
            or request.form.get("file_url", "")
            or request.form.get("link_url", "")
        ).strip()

        file = request.files.get("file")
        has_file = bool(file and file.filename)
        has_link = bool(submission_url)

        if has_link and not (
            submission_url.startswith("http://")
            or submission_url.startswith("https://")
        ):
            return jsonify({
                "success": False,
                "message": "กรุณาวางลิงก์ที่ขึ้นต้นด้วย http:// หรือ https://",
            })

        if has_file:
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)

            if file_size > app.config.get("MAX_CONTENT_LENGTH", 0):
                max_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024)
                return jsonify({
                    "success": False,
                    "message": f"ไฟล์ใหญ่เกิน {max_mb} MB กรุณาอัปโหลดไฟล์ไป Google Drive แล้วส่งเป็นลิงก์แทน",
                }), 413

        if not student_line_user_id or not assignment_id or (not has_file and not has_link):
            return jsonify({
                "success": False,
                "message": "กรุณาเลือกงาน และแนบไฟล์หรือวางลิงก์งาน",
            })

        student = get_student_by_line_user_id(student_line_user_id)
        if not student:
            return jsonify({
                "success": False,
                "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
            })

        assignment = get_assignment_by_id(assignment_id)
        if not assignment:
            return jsonify({
                "success": False,
                "message": "ไม่พบงานนี้",
            })

        classroom = str(student.get("classroom", "")).strip()
        assignment_classroom = str(assignment.get("classroom", "")).strip()

        if classroom != assignment_classroom:
            return jsonify({
                "success": False,
                "message": "งานนี้ไม่ใช่ของห้องคุณ",
            })

        assignment_title = str(assignment.get("title", "")).strip()
        due_date = str(assignment.get("due_date", "")).strip()
        due_time = assignment_due_time(assignment)
        allowed_exts = get_assignment_allowed_file_exts(assignment)
        allow_link_submission = assignment_allows_link(assignment)

        old_submission_row, old_submission = find_submission_row(student_line_user_id, assignment_id)
        if not old_submission:
            old_submission = get_submission_from_db(student_line_user_id, assignment_id)
        if old_submission:
            if not submission_edit_allowed(assignment):
                return jsonify({
                    "success": False,
                    "message": "คุณส่งงานนี้ไปแล้ว และเลยเวลาที่กำหนด ไม่สามารถแก้ไขได้",
                })

        if has_file:
            original_name = file.filename or "upload_file"

            ext = file_ext_from_name(original_name)
            if ext not in SUPPORTED_UPLOAD_EXTS:
                return jsonify({
                    "success": False,
                    "message": "ไม่รองรับไฟล์ประเภทนี้",
                })

            if not allowed_exts:
                return jsonify({
                    "success": False,
                    "message": "ส่งไม่ได้ งานนี้ครูกำหนดให้ส่งเป็นลิงก์เท่านั้น",
                })

            if ext not in allowed_exts:
                return jsonify({
                    "success": False,
                    "message": "ส่งไม่ได้ งานนี้รับเฉพาะไฟล์: " + allowed_file_exts_text(allowed_exts),
                })

            safe_name = f"{student.get('student_code', '')}_{student.get('student_name', '')}_{original_name}"

            ext = file_ext_from_name(original_name)
            file_url = ""
            old_file_url = str(old_submission.get("file_url", "")).strip() if old_submission else ""
            old_drive_file_id = drive_file_id_from_url(old_file_url)

            if ext in {"heic", "heif"}:
                file_bytes = file.read()
                file_bytes, safe_name = convert_heic_to_jpeg(file_bytes, safe_name)
                original_name = safe_name
                if old_drive_file_id:
                    try:
                        file_url = update_drive_file(
                            old_drive_file_id,
                            file_bytes=file_bytes,
                            file_name=safe_name,
                        )
                    except Exception as e:
                        print("[drive update existing file] Error:", e)

                if not file_url:
                    file_url = upload_file_to_drive(
                        file_bytes=file_bytes,
                        file_name=safe_name,
                        classroom=classroom,
                        assignment_title=assignment_title,
                    )
            else:
                # Use stream to avoid loading entire file into memory
                try:
                    file.stream.seek(0)
                except Exception:
                    pass

                if old_drive_file_id:
                    try:
                        file_url = update_drive_file(
                            old_drive_file_id,
                            file_stream=file.stream,
                            file_name=safe_name,
                        )
                    except Exception as e:
                        print("[drive update existing file] Error:", e)

                if not file_url:
                    try:
                        file.stream.seek(0)
                    except Exception:
                        pass
                    file_url = upload_file_to_drive(
                        file_stream=file.stream,
                        file_name=safe_name,
                        classroom=classroom,
                        assignment_title=assignment_title,
                    )
        else:
            if not allow_link_submission:
                return jsonify({
                    "success": False,
                    "message": "ส่งไม่ได้ งานนี้ครูกำหนดให้แนบไฟล์เท่านั้น",
                })

            original_name = "ส่งเป็นลิงก์"
            file_url = submission_url

        submitted_at = now_text()
        late = is_late_submission(due_date, submitted_at, due_time)
        auto_score = calculate_auto_submission_score(assignment, submitted_at)
        submission_db_payload = {
            "student_line_user_id": student_line_user_id,
            "assignment_id": assignment_id,
            "assignment_title": assignment_title,
            "classroom": classroom,
            "file_url": file_url,
            "file_name": original_name,
            "note": note,
            "late": late,
            "auto_score": auto_score,
            "submitted_at": submitted_at,
            "student_name": str(student.get("student_name", "")).strip(),
            "student_code": str(student.get("student_code", "")).strip(),
        }
        submission_db_saved = save_submission_to_db(submission_db_payload)

        if submission_db_saved:
            return jsonify({
                "success": True,
                "message": "รับงานเรียบร้อยแล้ว ระบบจะซิงก์เข้า Google Sheets อัตโนมัติ",
                "file_url": file_url,
                "auto_score": auto_score,
                "edited": bool(old_submission),
                "queued": True,
            })

        action = write_submission_to_sheets(submission_db_payload)
        update_student_rich_menu(student_line_user_id)

        return jsonify({
            "success": True,
            "message": "แก้ไขงานเรียบร้อยแล้ว" if action == "updated" else "ส่งงานเรียบร้อยแล้ว",
            "file_url": file_url,
            "auto_score": auto_score,
            "edited": action == "updated",
            "queued": False,
        })

    except Exception as e:
        print("[api_student_submit] Error:", e)
        if locals().get("submission_db_saved"):
            mark_db_sheets_status(
                "student_submissions",
                {
                    "student_line_user_id": locals().get("student_line_user_id", ""),
                    "assignment_id": locals().get("assignment_id", ""),
                },
                "sheet_failed",
                e,
            )
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/question-pin", methods=["POST"])
def api_teacher_question_pin():
    try:
        data = request.get_json()

        teacher_line_user_id = str(
            data.get("teacher_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        question_id = str(data.get("question_id", "")).strip()
        is_pinned = parse_bool(data.get("is_pinned", False), default=False)

        teacher = get_teacher_by_line_user_id(teacher_line_user_id)
        if not teacher:
            return jsonify({
                "success": False,
                "message": "ไม่มีสิทธิ์ครู",
            })

        if not question_id:
            return jsonify({
                "success": False,
                "message": "ไม่พบคำถามนี้",
            })

        ws = get_worksheet("questions")
        headers = ensure_headers(ws, BASE_SHEETS["questions"])
        records = get_sheet_records("questions")
        rooms = get_teacher_rooms(teacher_line_user_id)

        for i, r in enumerate(records, start=2):
            if str(r.get("question_id", "")).strip() != question_id:
                continue

            classroom = normalize_classroom_text(r.get("classroom", ""))
            if classroom not in rooms:
                return jsonify({
                    "success": False,
                    "message": "คุณไม่มีสิทธิ์แก้คำถามของห้องนี้",
                })

            pin_target_rooms = []
            if is_pinned:
                if (
                    str(r.get("status", "")).strip() != "answered"
                    or not str(r.get("answer_text", "")).strip()
                ):
                    return jsonify({
                        "success": False,
                        "message": "กรุณาตอบคำถามก่อนปักหมุด",
                    })

                pin_target_rooms = parse_pin_target_rooms(data, classroom)
                pin_error = validate_pin_target_rooms(pin_target_rooms, rooms)
                if pin_error:
                    return jsonify({
                        "success": False,
                        "message": pin_error,
                    })

            pinned_at = ""
            if is_pinned:
                pinned_at = str(r.get("pinned_at", "")).strip() or now_text()

            ws.batch_update([
                {
                    "range": f"{col_letter(headers.index('is_pinned') + 1)}{i}",
                    "values": [["yes" if is_pinned else "no"]],
                },
                {
                    "range": f"{col_letter(headers.index('pinned_at') + 1)}{i}",
                    "values": [[pinned_at]],
                },
                {
                    "range": f"{col_letter(headers.index('pinned_by') + 1)}{i}",
                    "values": [[teacher_line_user_id if is_pinned else ""]],
                },
                {
                    "range": f"{col_letter(headers.index('pinned_classrooms') + 1)}{i}",
                    "values": [[" ".join(pin_target_rooms) if is_pinned else ""]],
                },
            ], value_input_option="USER_ENTERED")
            invalidate_sheet_cache("questions")

            return jsonify({
                "success": True,
                "message": "อัปเดตห้องปักหมุดแล้ว" if is_pinned else "ลบออกจากปักหมุดแล้ว",
                "is_pinned": is_pinned,
                "pinned_classrooms": pin_target_rooms,
            })

        return jsonify({
            "success": False,
            "message": "ไม่พบคำถามนี้",
        })

    except Exception as e:
        print("[api_teacher_question_pin] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/student/question", methods=["POST"])
def api_student_question():
    try:
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            data = request.form
        else:
            data = request.get_json() or {}

        student_line_user_id = str(
            data.get("student_line_user_id", "") or data.get("line_user_id", "")
        ).strip()
        question_text = str(data.get("question_text", "")).strip()
        attachment_url = str(data.get("attachment_url", "") or data.get("file_url", "")).strip()
        attachment_name = ""

        if not student_line_user_id or not question_text:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกคำถาม",
            })

        student = get_student_by_line_user_id(student_line_user_id)
        if not student:
            return jsonify({
                "success": False,
                "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
            })

        question_id = "q_" + uuid.uuid4().hex[:12]
        classroom = str(student.get("classroom", "")).strip()
        student_name = str(student.get("student_name", "")).strip()

        file = request.files.get("file")
        has_file = bool(file and file.filename)
        if has_file:
            original_name = file.filename or "question_file"
            ext = file_ext_from_name(original_name)
            if ext not in SUPPORTED_UPLOAD_EXTS:
                return jsonify({
                    "success": False,
                    "message": "ไม่รองรับไฟล์ประเภทนี้",
                })

            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)
            if file_size > app.config.get("MAX_CONTENT_LENGTH", 0):
                max_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024)
                return jsonify({
                    "success": False,
                    "message": f"ไฟล์ใหญ่เกิน {max_mb} MB กรุณาอัปโหลดขึ้น Google Drive แล้ววางลิงก์แทน",
                }), 413

            safe_name = f"{student.get('student_code', '')}_{student_name}_{original_name}"
            ext = file_ext_from_name(original_name)
            if ext in {"heic", "heif"}:
                file_bytes = file.read()
                file_bytes, safe_name = convert_heic_to_jpeg(file_bytes, safe_name)
                original_name = safe_name
                attachment_url = upload_file_to_drive(
                    file_bytes=file_bytes,
                    file_name=safe_name,
                    classroom=classroom,
                    assignment_title="คำถามนักเรียน",
                )
            else:
                try:
                    file.stream.seek(0)
                except Exception:
                    pass
                attachment_url = upload_file_to_drive(
                    file_stream=file.stream,
                    file_name=safe_name,
                    classroom=classroom,
                    assignment_title="คำถามนักเรียน",
                )
            attachment_name = original_name
        elif attachment_url:
            attachment_name = "ลิงก์แนบ"

        ws = get_worksheet("questions")
        headers = ensure_headers(ws, BASE_SHEETS["questions"])

        buffered_append_row(
            ws,
            row_values_for_headers(headers, {
                "question_id": question_id,
                "created_at": now_text(),
                "student_line_user_id": student_line_user_id,
                "student_name": student_name,
                "classroom": classroom,
                "question_text": question_text,
                "attachment_url": attachment_url,
                "attachment_name": attachment_name,
                "status": "pending",
                "answer_text": "",
                "answered_at": "",
                "answered_by": "",
                "student_seen": "",
                "is_pinned": "no",
                "pinned_at": "",
                "pinned_by": "",
            }),
            value_input_option="USER_ENTERED",
        )
        invalidate_sheet_cache("submissions")
        invalidate_sheet_cache("questions")

        # อัปเดต rich menu ครูที่ดูแลห้องนี้
        teachers_ws = get_worksheet("teachers")
        teachers = get_sheet_records("teachers")
        for t in teachers:
            rooms = normalize_rooms_text(t.get("rooms", ""))
            if classroom in rooms:
                tid = str(t.get("teacher_line_user_id", "")).strip()
                update_teacher_rich_menu(tid)

        return jsonify({
            "success": True,
            "message": "ส่งคำถามเรียบร้อยแล้ว",
        })

    except Exception as e:
        print("[api_student_question] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/student/my-questions")
def api_student_my_questions():
    student_line_user_id = request.args.get("student_line_user_id", "").strip()

    ws = get_worksheet("questions")
    headers = ensure_headers(ws, BASE_SHEETS["questions"])
    records = get_sheet_records("questions")

    result = []
    row_indexes_to_mark = []

    for i, r in enumerate(records, start=2):
        if str(r.get("student_line_user_id", "")).strip() == student_line_user_id:
            result.append(r)
            if str(r.get("status", "")).strip() == "answered":
                row_indexes_to_mark.append(i)

    # เมื่อนักเรียนเปิดดูแล้ว ให้ mark ว่าเห็นคำตอบแล้ว
    if "student_seen" in headers:
        col = headers.index("student_seen") + 1
        updates = [
            {
                "range": f"{col_letter(col)}{i}",
                "values": [["yes"]],
            }
            for i in row_indexes_to_mark
        ]
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            invalidate_sheet_cache("questions")

    update_student_rich_menu(student_line_user_id)

    return jsonify({
        "success": True,
        "questions": result,
    })


@app.route("/api/student/classroom-questions")
def api_student_classroom_questions():
    student_line_user_id = request.args.get("student_line_user_id", "").strip()

    student = get_student_by_line_user_id(student_line_user_id)
    if not student:
        return jsonify({
            "success": False,
            "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
            "questions": [],
        })

    classroom = normalize_classroom_text(student.get("classroom", ""))
    if not classroom:
        return jsonify({
            "success": False,
            "message": "ยังไม่พบข้อมูลห้องเรียนของนักเรียน",
            "questions": [],
        })

    ws = get_worksheet("questions")
    headers = ensure_headers(ws, BASE_SHEETS["questions"])
    records = get_sheet_records("questions")

    questions = []
    row_indexes_to_mark = []
    for i, r in enumerate(records, start=2):
        if (
            str(r.get("status", "")).strip() == "answered"
            and str(r.get("answer_text", "")).strip()
            and question_is_pinned_for_classroom(r, classroom)
        ):
            questions.append(question_public_payload(r, classroom))
            if str(r.get("student_line_user_id", "")).strip() == student_line_user_id:
                row_indexes_to_mark.append(i)

    if "student_seen" in headers:
        col = headers.index("student_seen") + 1
        updates = [
            {
                "range": f"{col_letter(col)}{i}",
                "values": [["yes"]],
            }
            for i in row_indexes_to_mark
        ]
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            invalidate_sheet_cache("questions")

    update_student_rich_menu(student_line_user_id)

    questions.sort(
        key=lambda q: q.get("pinned_at") or q.get("answered_at") or q.get("created_at"),
        reverse=True,
    )

    return jsonify({
        "success": True,
        "classroom": classroom,
        "questions": questions[:50],
    })


@app.route("/api/student/announcements")
def api_student_announcements():
    student_line_user_id = request.args.get("student_line_user_id", "").strip()

    student = get_student_by_line_user_id(student_line_user_id)
    if not student:
        return jsonify({
            "success": False,
            "message": "ยังไม่พบข้อมูลนักเรียน กรุณาลงทะเบียนก่อน",
        })

    classroom = str(student.get("classroom", "")).strip()

    records = get_sheet_records("announcements")

    result = [
        r for r in records
        if str(r.get("classroom", "")).strip() == classroom
    ]

    result = list(reversed(result))

    return jsonify({
        "success": True,
        "announcements": result,
    })


# =========================================================
# Student Group Menu
# =========================================================

def make_student_group_menu_text():
    return (
        "เมนูนักเรียน\n\n"
        "กรุณาเปิดลิงก์ผ่าน LINE\n\n"
        f"ลงทะเบียน:\nhttps://liff.line.me/{LIFF_STUDENT_REGISTER_ID}\n\n"
        f"ส่งงาน:\nhttps://liff.line.me/{LIFF_STUDENT_SUBMIT_ID}\n\n"
        f"งานค้าง:\nhttps://liff.line.me/{LIFF_STUDENT_PENDING_ID}\n\n"
        f"ถามคำถาม:\nhttps://liff.line.me/{LIFF_STUDENT_QUESTION_ID}\n\n"
        f"ประกาศ:\nhttps://liff.line.me/{LIFF_STUDENT_ANNOUNCE_ID}"
    )


def make_student_group_flex_menu():
    return {
        "type": "flex",
        "altText": "เมนูนักเรียน",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "เมนูนักเรียน",
                        "weight": "bold",
                        "size": "xl",
                        "align": "center",
                    },
                    {
                        "type": "text",
                        "text": "เลือกเมนูที่ต้องการใช้งาน",
                        "size": "sm",
                        "color": "#666666",
                        "align": "center",
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "uri",
                            "label": "ลงทะเบียน",
                            "uri": liff_url(LIFF_STUDENT_REGISTER_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "uri",
                            "label": "ส่งงาน",
                            "uri": liff_url(LIFF_STUDENT_SUBMIT_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "งานค้าง",
                            "uri": liff_url(LIFF_STUDENT_PENDING_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "ถามคำถาม",
                            "uri": liff_url(LIFF_STUDENT_QUESTION_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "ประกาศ",
                            "uri": liff_url(LIFF_STUDENT_ANNOUNCE_ID),
                        },
                    },
                ],
            },
        },
    }



def make_teacher_flex_menu():
    return {
        "type": "flex",
        "altText": "เมนูครู",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "เมนูครู",
                        "weight": "bold",
                        "size": "xl",
                        "align": "center",
                    },
                    {
                        "type": "text",
                        "text": "เลือกเมนูที่ต้องการใช้งาน",
                        "size": "sm",
                        "color": "#666666",
                        "align": "center",
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "uri",
                            "label": "ตั้งค่าห้องที่ดูแล",
                            "uri": liff_url(LIFF_TEACHER_SETUP_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "uri",
                            "label": "สั่งงาน",
                            "uri": liff_url(LIFF_TEACHER_ASSIGNMENT_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "งานค้าง",
                            "uri": liff_url(LIFF_TEACHER_PENDING_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "คำถามนักเรียน",
                            "uri": liff_url(LIFF_TEACHER_QUESTIONS_ID),
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "ประกาศ",
                            "uri": liff_url(LIFF_TEACHER_ANNOUNCE_ID),
                        },
                    },
                ],
            },
        },
    }


# =========================================================
# Deadline Reminder
# =========================================================

LINE_MENTION_LIMIT_PER_MESSAGE = 20
LINE_PUSH_MESSAGES_PER_REQUEST = 5
DEADLINE_LOG_TYPE_NOTICE = "deadline_notice"
DEADLINE_LOG_TYPE_PENDING = "pending_reminder"


def deadline_log_exists(assignment_id, classroom, notification_type=None, count_legacy=False):
    assignment_id = str(assignment_id or "").strip()
    classroom = normalize_classroom_text(classroom)
    notification_type = str(notification_type or "").strip()

    if not assignment_id or not classroom:
        return False

    for r in get_sheet_records("deadline_logs"):
        if (
            str(r.get("assignment_id", "")).strip() != assignment_id
            or normalize_classroom_text(r.get("classroom", "")) != classroom
        ):
            continue

        row_type = str(r.get("notification_type", "")).strip()
        if not notification_type:
            return True
        if row_type == notification_type:
            return True
        if count_legacy and not row_type:
            return True

    return False


def append_deadline_log(assignment_id, classroom, group_id, message, notification_type):
    ws = get_worksheet("deadline_logs")
    headers = ensure_headers(ws, BASE_SHEETS["deadline_logs"])
    buffered_append_row(
        ws,
        row_values_for_headers(headers, {
            "log_id": "dl_" + uuid.uuid4().hex[:12],
            "created_at": now_text(),
            "assignment_id": str(assignment_id or "").strip(),
            "classroom": normalize_classroom_text(classroom),
            "group_id": str(group_id or "").strip(),
            "notification_type": str(notification_type or "").strip(),
            "message": str(message or ""),
        }),
        value_input_option="RAW",
    )
    invalidate_sheet_cache("deadline_logs")


def student_submission_from_index(student, assignment_id, submission_index):
    for sid in student_line_user_ids_for_record(student):
        sub = submission_index.get((sid, assignment_id))
        if sub:
            return sub
    return None


def student_notification_line_user_id(student):
    ids = student_line_user_ids_for_record(student)
    return ids[0] if ids else ""


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def pending_student_label(student):
    code = str(student.get("student_code", "")).strip()
    name = str(student.get("student_name", "")).strip() or "นักเรียน"

    if code:
        return f"เลขที่ {code} {name}"
    return name


def escape_text_v2(value):
    return str(value or "").replace("{", "{{").replace("}", "}}")


def build_deadline_mention_messages(classroom, assignment, pending_students):
    title = str(assignment.get("title", "")).strip()
    due_text = assignment_due_text(assignment) or "-"

    if not pending_students:
        return [{
            "type": "text",
            "text": (
                f"แจ้งเตือนส่งงาน ห้อง {classroom}\n\n"
                f"งาน: {title}\n"
                f"กำหนดส่ง: {due_text}\n\n"
                "ไม่มีนักเรียนค้างส่งแล้ว"
            ),
            "notificationDisabled": False,
        }]

    student_chunks = list(chunks(pending_students, LINE_MENTION_LIMIT_PER_MESSAGE))
    messages = []
    total_chunks = len(student_chunks)

    for chunk_index, student_chunk in enumerate(student_chunks, start=1):
        has_mentions = any(
            student_notification_line_user_id(student)
            for student in student_chunk
        )
        classroom_text = escape_text_v2(classroom) if has_mentions else str(classroom)
        title_text = escape_text_v2(title) if has_mentions else title
        due_text_display = escape_text_v2(due_text) if has_mentions else due_text
        chunk_title = ""
        if total_chunks > 1:
            chunk_title = f" ชุด {chunk_index}/{total_chunks}"

        text = (
            f"แจ้งเตือนส่งงาน ห้อง {classroom_text}{chunk_title}\n\n"
            f"งาน: {title_text}\n"
            f"กำหนดส่ง: {due_text_display}\n\n"
            "นักเรียนที่ยังไม่ส่ง:\n"
        )
        substitution = {}

        for student_index, student in enumerate(student_chunk, start=1):
            user_id = student_notification_line_user_id(student)
            label = pending_student_label(student)
            label_text = escape_text_v2(label) if has_mentions else label

            if user_id:
                key = f"m{chunk_index}_{student_index}"
                text += f"- {label_text} {{{key}}}\n"
                substitution[key] = {
                    "type": "mention",
                    "mentionee": {
                        "type": "user",
                        "userId": user_id,
                    },
                }
            else:
                text += f"- {label_text} (ยังไม่ได้ผูก LINE)\n"

        if substitution:
            messages.append({
                "type": "textV2",
                "text": text,
                "substitution": substitution,
                "notificationDisabled": False,
            })
        else:
            messages.append({
                "type": "text",
                "text": text,
                "notificationDisabled": False,
            })

    return messages


def build_deadline_mention_message(classroom, assignment, pending_students):
    messages = build_deadline_mention_messages(classroom, assignment, pending_students)
    return messages[0] if messages else {
        "type": "text",
        "text": "ไม่มีนักเรียนค้างส่งแล้ว",
        "notificationDisabled": False,
    }


def push_message_batches(to, messages):
    results = []
    for message_batch in chunks(messages, LINE_PUSH_MESSAGES_PER_REQUEST):
        res = push_messages(to, message_batch)
        results.append({
            "status": res.status_code if res else None,
            "text": res.text if res else None,
            "message_count": len(message_batch),
        })
    return results


def deadline_messages_log_text(messages):
    parts = []
    for message in messages:
        text = str(message.get("text", "")).strip()
        if text:
            parts.append(text)
    return "\n\n---\n\n".join(parts)


def build_deadline_private_text(classroom, assignment, overdue=False):
    title = str(assignment.get("title", "")).strip()
    due_text = assignment_due_text(assignment) or "-"
    submit_url = liff_url(LIFF_STUDENT_SUBMIT_ID)

    heading = "แจ้งเตือนงานค้าง" if overdue else "แจ้งเตือนส่งงานวันนี้"
    closing = "กรุณาส่งงานโดยเร็ว" if overdue else "กรุณาส่งงานภายในวันนี้"

    text = (
        f"{heading}\n\n"
        f"ห้อง: {classroom}\n"
        f"งาน: {title}\n"
        f"กำหนดส่ง: {due_text}\n\n"
        f"{closing}"
    )

    if submit_url:
        text += f"\n\nส่งงาน:\n{submit_url}"

    return text


def notify_deadline_for_assignment(
    assignment_id,
    only_due_today=False,
    skip_if_logged=False,
    notification_type=DEADLINE_LOG_TYPE_PENDING,
):
    assignment = get_assignment_by_id(assignment_id)
    if not assignment:
        return {
            "success": False,
            "message": "ไม่พบ assignment",
        }

    if not assignment_requires_submission(assignment):
        return {
            "success": True,
            "message": "skipped_not_submission_assignment",
            "assignment_id": assignment_id,
        }

    classroom = normalize_classroom_text(assignment.get("classroom", ""))
    due_date_text = str(assignment.get("due_date", "")).strip()
    due = parse_date(due_date_text)

    if only_due_today and due != today_date():
        return {
            "success": True,
            "message": "skipped_not_due_today",
            "assignment_id": assignment_id,
            "classroom": classroom,
            "due_date": due_date_text,
            "due_time": assignment_due_time(assignment),
            "today": today_date().strftime("%Y-%m-%d"),
        }

    if skip_if_logged and deadline_log_exists(assignment_id, classroom, notification_type):
        return {
            "success": True,
            "message": "skipped_already_sent",
            "assignment_id": assignment_id,
            "classroom": classroom,
            "notification_type": notification_type,
        }

    students = get_students_by_classroom(classroom)
    student_line_user_ids = student_line_user_ids_for_records(students)
    submission_index = get_submissions_index(
        classroom=classroom,
        assignment_ids=[assignment_id],
        student_line_user_ids=student_line_user_ids,
    )

    pending_students = []
    for s in students:
        sub = student_submission_from_index(s, assignment_id, submission_index)
        if not sub:
            pending_students.append(s)

    if not pending_students:
        return {
            "success": True,
            "message": "skipped_no_pending",
            "assignment_id": assignment_id,
            "classroom": classroom,
            "pending_count": 0,
        }

    group_id = get_class_group_id(classroom)
    group_messages = build_deadline_mention_messages(classroom, assignment, pending_students)
    group_results = []

    if group_id:
        group_results = push_message_batches(group_id, group_messages)

    due_dt = assignment_due_datetime(assignment)
    overdue = bool(due_dt and now_dt().replace(tzinfo=None) > due_dt)
    private_text = build_deadline_private_text(classroom, assignment, overdue=overdue)
    private_results = []

    for s in pending_students:
        sid = student_notification_line_user_id(s)
        student_name = str(s.get("student_name", "")).strip()

        if not sid:
            private_results.append({
                "student_name": student_name,
                "status": None,
                "message": "missing_student_line_user_id",
            })
            continue

        private_res = push_message(sid, private_text)
        private_results.append({
            "student_name": student_name,
            "status": private_res.status_code if private_res else None,
            "text": private_res.text if private_res else None,
        })

    private_sent_count = sum(
        1
        for r in private_results
        if r.get("status") and 200 <= int(r.get("status")) < 300
    )

    # log
    try:
        append_deadline_log(
            assignment_id,
            classroom,
            group_id,
            deadline_messages_log_text(group_messages) + f"\n\nส่งไลน์ส่วนตัวสำเร็จ {private_sent_count}/{len(pending_students)} คน",
            notification_type,
        )
    except Exception as e:
        print("[deadline log] Error:", e)

    return {
        "success": True,
        "message": "sent",
        "assignment_id": assignment_id,
        "classroom": classroom,
        "due_date": due_date_text,
        "due_time": assignment_due_time(assignment),
        "pending_count": len(pending_students),
        "group_id": group_id,
        "group_message_count": len(group_messages),
        "group_status": group_results[0].get("status") if group_results else None,
        "group_text": group_results[0].get("text") if group_results else None,
        "group_results": group_results,
        "private_sent_count": private_sent_count,
        "private_results": private_results,
        "notification_type": notification_type,
    }


@app.route("/debug/notify-deadline")
def debug_notify_deadline():
    if not require_debug_secret():
        return jsonify({"success": False, "message": "debug endpoint is disabled"}), 403

    assignment_id = request.args.get("assignment_id", "").strip()
    result = notify_deadline_for_assignment(assignment_id)
    return jsonify(result)


@app.route("/cron/deadline-reminder")
def cron_deadline_reminder():
    secret = request.args.get("secret", "").strip()

    if CRON_SECRET and secret != CRON_SECRET:
        return jsonify({
            "success": False,
            "message": "invalid secret",
        }), 403

    target_date = today_date()
    include_overdue = parse_bool(request.args.get("include_overdue", "1"), default=True)
    force = parse_bool(request.args.get("force", ""), default=False)

    records = get_sheet_records("assignments")

    results = []
    skipped = 0

    for a in records:
        assignment = add_assignment_due_metadata(dict(a))
        due_date_text = str(a.get("due_date", "")).strip()
        due = parse_date(due_date_text)

        if not due:
            skipped += 1
            continue

        if include_overdue:
            should_notify = due <= target_date
        else:
            should_notify = due == target_date

        if not should_notify:
            skipped += 1
            continue

        if not assignment_requires_submission(assignment):
            skipped += 1
            continue

        assignment_id = str(assignment.get("assignment_id", "")).strip()
        if not assignment_id:
            skipped += 1
            continue

        result = notify_deadline_for_assignment(
            assignment_id,
            only_due_today=not include_overdue,
            skip_if_logged=not force,
            notification_type=DEADLINE_LOG_TYPE_PENDING,
        )
        results.append(result)

    return jsonify({
        "success": True,
        "target_date": target_date.strftime("%Y-%m-%d"),
        "include_overdue": include_overdue,
        "force": force,
        "skipped": skipped,
        "count": len(results),
        "sent": len([r for r in results if r.get("message") == "sent"]),
        "results": results,
    })


@app.route("/cron/sync-db-to-sheets")
def cron_sync_db_to_sheets():
    secret = request.args.get("secret", "").strip()

    if CRON_SECRET and secret != CRON_SECRET:
        return jsonify({
            "success": False,
            "message": "invalid secret",
        }), 403

    limit = request.args.get("limit", "50").strip()
    return jsonify(sync_db_to_sheets(limit))


# Debug: test push endpoint (protected by CRON_SECRET if set)
@app.route("/debug/send-test-line", methods=["GET"])
def debug_send_test_line():
    secret = request.args.get("secret", "")
    to = request.args.get("to", "")
    text = request.args.get("text", "Test message from debug endpoint")

    # If CRON_SECRET is set in env, require it to avoid open abuse
    cron_secret = os.getenv("CRON_SECRET", "")
    if cron_secret and secret != cron_secret:
        return jsonify({"success": False, "message": "missing or invalid secret"}), 403

    if not to:
        return jsonify({"success": False, "message": "missing 'to' parameter (userId or groupId)"}), 400

    res = push_message(to, text)
    if not res:
        return jsonify({"success": False, "message": "request failed (exception)"}), 500

    return jsonify({
        "success": True,
        "status_code": getattr(res, "status_code", None),
        "response_text": getattr(res, "text", None),
    }), res.status_code if hasattr(res, "status_code") else 200


# =========================================================
# Webhook
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json()
    print("[webhook body]", body)

    events = body.get("events", [])

    for event in events:
        event_type = event.get("type")
        reply_token = event.get("replyToken", "")
        source = event.get("source", {})
        source_type = source.get("type", "")
        user_id = source.get("userId", "")
        room_id = source.get("roomId", "")
        group_id = source.get("groupId", "")
        chat_id = group_id or room_id

        # -------------------------------------------------
        # Follow: คนแอดบอทส่วนตัว
        # -------------------------------------------------
        if event_type == "follow":
            teacher = get_teacher_by_line_user_id(user_id)
            student = get_student_by_line_user_id(user_id)

            if teacher:
                update_teacher_rich_menu(user_id)
                reply_message(
                    reply_token,
                    "ยินดีต้อนรับครูเข้าสู่ระบบ\n\n"
                    "พิมพ์ 'เมนู' เพื่ออัปเดตเมนูครู"
                )

            elif student:
                # นักเรียน → ใช้เมนูนักเรียนปกติ
                update_student_rich_menu(user_id)
                reply_message(
                    reply_token,
                    "ยินดีต้อนรับนักเรียน\n\n"
                    "สามารถใช้งานเมนูด้านล่างได้เลย"
                )

            else:
                # คนใหม่ทุกคน → ให้เมนูลงทะเบียนนักเรียนก่อน
                if STUDENT_RICH_MENU_REGISTER_ID:
                    link_rich_menu_to_user(user_id, STUDENT_RICH_MENU_REGISTER_ID)

                reply_message(
                    reply_token,
                    "ยินดีต้อนรับ\n\n"
                    f"{student_register_prompt_text()}\n\n"
                    "ถ้าคุณเป็นครู ให้พิมพ์:\n"
                    "ตั้งค่าครู"
                )

        # -------------------------------------------------
        # Join group
        # -------------------------------------------------
        elif event_type == "join":
            reply_message(
                reply_token,
                "บอทเข้ากลุ่มแล้ว\n\n"
                "ถ้าต้องการผูกกลุ่มกับห้องเรียน ให้ครูพิมพ์:\n"
                "ผูกห้อง 401\n\n"
                "ถ้าต้องการเมนูนักเรียน ให้พิมพ์:\n"
                "เมนูนักเรียน"
            )

        # -------------------------------------------------
        # Message
        # -------------------------------------------------
        elif event_type == "message":
            message = event.get("message", {})
            message_type = message.get("type", "")

            if message_type != "text":
                continue

            text = message.get("text", "").strip()
            command = text.upper()

            # -----------------------------
            # คำสั่งในกลุ่มหรือห้องแชทกลุ่ม
            # -----------------------------
            if source_type in {"group", "room"}:
                if text.startswith("ผูกห้อง"):
                    classroom = normalize_classroom_text(text.replace("ผูกห้อง", "").strip())
                    _, access_error = validate_teacher_classroom_access(
                        user_id,
                        classroom,
                        "กรุณาพิมพ์รูปแบบ:\nผูกห้อง 401",
                    )

                    if access_error:
                        reply_message(reply_token, access_error)
                    else:
                        upsert_class_group(classroom, chat_id)
                        reply_message(
                            reply_token,
                            f"ผูกกลุ่มนี้กับห้อง {classroom} เรียบร้อยแล้ว"
                        )

                elif text in ["เมนู", "เมนูนักเรียน", "menu"]:
                    flex = make_student_group_flex_menu()
                    reply_messages(reply_token, [flex])

                elif (
                    command in ["UU", "SS"]
                    or command.startswith("UU ")
                    or command.startswith("UU:")
                    or command.startswith("UU：")
                    or command.startswith("UU-")
                    or command.startswith("SS ")
                    or command.startswith("SS:")
                    or command.startswith("SS：")
                    or command.startswith("SS-")
                    or text.startswith("ทดสอบแจ้งเตือน")
                    or text.startswith("อัปเดตชีต")
                    or text.startswith("อัพเดตชีต")
                    or text.startswith("ซิงก์คะแนน")
                    or text.startswith("ซิ้งคะแนน")
                    or text.startswith("sync คะแนน")
                ):
                    reply_message(
                        reply_token,
                        "คำสั่งนี้ให้ครูพิมพ์ในแชทส่วนตัวกับบอทเท่านั้น"
                    )

                else:
                    # ไม่ตอบทุกข้อความในกลุ่ม เพื่อไม่ให้รบกวน
                    pass

            # -----------------------------
            # คำสั่งในแชทส่วนตัว
            # -----------------------------
            else:
                teacher = get_teacher_by_line_user_id(user_id)
                student = get_student_by_line_user_id(user_id)

                if text in ["เช็กไอดี", "เช็คไอดี", "id", "ID"]:
                    rooms = get_teacher_rooms(user_id) if teacher else []
                    matched_teachers = get_teacher_records_by_line_user_id(user_id)
                    reply_message(
                        reply_token,
                        "ข้อมูลที่ webhook เห็น\n"
                        f"userId: {user_id or '-'}\n"
                        f"สถานะครู: {'พบ' if teacher else 'ไม่พบ'}\n"
                        f"จำนวนแถวครูที่เจอ: {len(matched_teachers)}\n"
                        f"ห้องที่อ่านได้: {', '.join(rooms) if rooms else '-'}"
                    )

                elif text in ["เมนู", "menu", "รีเซ็ตเมนู", "reset menu"]:
                    if teacher:
                        unlink_rich_menu_from_user(user_id)
                        update_teacher_rich_menu(user_id)
                        reply_messages(reply_token, [
                            {
                                "type": "text",
                                "text": "รีเซ็ตและอัปเดตเมนูครูเรียบร้อยแล้ว\n\nถ้าเปิดในคอม ให้ใช้ปุ่มเมนูด้านล่างนี้แทน Rich Menu"
                            },
                            make_teacher_flex_menu()
                        ])
                    elif student:
                        update_student_rich_menu(user_id)
                        reply_messages(reply_token, [
                            {
                                "type": "text",
                                "text": "อัปเดตเมนูนักเรียนเรียบร้อยแล้ว\n\nถ้า Rich Menu ยังไม่ขึ้น ให้ปิดคีย์บอร์ดแล้วเปิดใหม่"
                            },
                            make_student_group_flex_menu()
                        ])
                    else:
                        update_student_rich_menu(user_id)
                        reply_message(
                            reply_token,
                            student_register_prompt_text() + "\n\n"
                            "ถ้าเป็นครู ให้พิมพ์ 'ตั้งค่าครู'"
                        )

                elif text in ["ลงทะเบียน", "สมัคร", "register"]:
                    if teacher:
                        reply_message(
                            reply_token,
                            "บัญชีนี้เป็นครูในระบบแล้ว\n\n"
                            "พิมพ์ 'เมนู' เพื่ออัปเดตเมนูครู"
                        )
                    elif student:
                        update_student_rich_menu(user_id)
                        reply_message(
                            reply_token,
                            "บัญชีนี้ลงทะเบียนนักเรียนแล้ว\n\n"
                            "พิมพ์ 'เมนู' เพื่อเปิดเมนูนักเรียน"
                        )
                    else:
                        update_student_rich_menu(user_id)
                        reply_message(reply_token, student_register_prompt_text())

                elif text in ["ตั้งค่าครู", "ครู"]:
                    reply_message(
                        reply_token,
                        f"เปิดหน้าตั้งค่าครูผ่านลิงก์นี้:\n{liff_url(LIFF_TEACHER_SETUP_ID)}\n\n"
                        "ต้องกรอกรหัสครูที่ได้รับจากผู้ดูแล"
                    )

                elif text.startswith("ผูกห้อง"):
                    reply_message(
                        reply_token,
                        "คำสั่งผูกห้องต้องพิมพ์ในกลุ่มห้องเรียน เพราะระบบต้องอ่าน groupId ของกลุ่มนั้น"
                    )

                elif text.startswith("ทดสอบแจ้งเตือน"):
                    assignment_id = text.replace("ทดสอบแจ้งเตือน", "").strip()
                    if not teacher:
                        reply_message(reply_token, "คำสั่งนี้ใช้ได้เฉพาะครู")
                    elif not assignment_id:
                        reply_message(reply_token, "กรุณาพิมพ์ เช่น:\nทดสอบแจ้งเตือน as_xxxxx")
                    else:
                        assignment = get_assignment_by_id(assignment_id)
                        if not assignment:
                            reply_message(reply_token, "ไม่พบงานนี้")
                        else:
                            classroom = str(assignment.get("classroom", "")).strip()
                            _, access_error = validate_teacher_classroom_access(
                                user_id,
                                classroom,
                                "ไม่พบห้องของงานนี้",
                            )
                            if access_error:
                                reply_message(reply_token, access_error)
                            else:
                                try:
                                    result = notify_deadline_for_assignment(assignment_id)
                                    reply_message(
                                        reply_token,
                                        "ผลทดสอบแจ้งเตือน\n"
                                        f"สถานะ: {result.get('message', '-')}\n"
                                        f"ห้อง: {result.get('classroom', classroom)}\n"
                                        f"ค้างส่ง: {result.get('pending_count', 0)} คน\n"
                                        f"ส่งในกลุ่ม: {result.get('group_message_count', 0)} ข้อความ\n"
                                        f"ส่งส่วนตัวสำเร็จ: {result.get('private_sent_count', 0)} คน"
                                    )
                                except Exception as e:
                                    reply_message(reply_token, f"ทดสอบแจ้งเตือนไม่สำเร็จ กรุณารอสักครู่แล้วลองใหม่\n{str(e)}")

                elif (
                    command == "UU"
                    or command.startswith("UU ")
                    or command.startswith("UU:")
                    or command.startswith("UU：")
                    or command.startswith("UU-")
                ):
                    if not teacher:
                        reply_message(reply_token, "คำสั่งนี้ใช้ได้เฉพาะครู")
                    else:
                        _, classroom = parse_room_scoped_command(text, ["UU"])
                        if classroom:
                            try:
                                result = update_classroom_sheet_for_teacher(user_id, classroom)
                                reply_message(
                                    reply_token,
                                    result.get("message", "อัปเดตชีตไม่สำเร็จ")
                                )
                            except Exception as e:
                                reply_message(reply_token, f"อัปเดตชีตห้อง {classroom} ไม่สำเร็จ\n{str(e)}")
                        else:
                            rooms = get_teacher_rooms(user_id)
                            result = update_all_classroom_sheets_for_teacher(user_id, rooms)
                            if not result.get("rooms"):
                                reply_message(
                                    reply_token,
                                    result.get("message", "อัปเดตชีตไม่สำเร็จ")
                                    + f"\n\nuserId: {user_id or '-'}"
                                )
                            else:
                                target_count = len(result.get("target_rooms", result.get("rooms", [])))
                                text_lines = []
                                if target_count:
                                    text_lines.extend([
                                        "อัปเดตชีตทุกห้องที่มีข้อมูลเปลี่ยนแล้ว",
                                        f"สำเร็จ {len(result.get('updated', []))}/{target_count} ห้อง",
                                    ])
                                else:
                                    text_lines.append("ไม่มีห้องที่ต้องอัปเดตตอนนี้")
                                text_lines.append("ถ้าต้องการบังคับอัปเดต ใช้ UU ตามด้วยเลขห้อง เช่น UU 401")
                                if result.get("updated"):
                                    text_lines.append("ห้องที่อัปเดต: " + ", ".join(result["updated"]))
                                if result.get("skipped"):
                                    text_lines.append("ข้ามห้องที่ไม่มีข้อมูลเปลี่ยน: " + ", ".join(result["skipped"]))
                                if result.get("failed"):
                                    failed_rooms = [f"{r['classroom']} ({r['message']})" for r in result["failed"]]
                                    text_lines.append("ไม่สำเร็จ: " + ", ".join(failed_rooms))
                                reply_message(reply_token, "\n".join(text_lines))

                elif (
                    command == "SS"
                    or command.startswith("SS ")
                    or command.startswith("SS:")
                    or command.startswith("SS：")
                    or command.startswith("SS-")
                ):
                    if not teacher:
                        reply_message(reply_token, "คำสั่งนี้ใช้ได้เฉพาะครู")
                    else:
                        _, classroom = parse_room_scoped_command(text, ["SS"])
                        if classroom:
                            try:
                                result = sync_classroom_scores_for_teacher(user_id, classroom)
                                if result.get("success"):
                                    reply_message(
                                        reply_token,
                                        f"ซิงก์คะแนนห้อง {classroom} เสร็จแล้ว\n"
                                        f"อัปเดต: {result.get('updated', 0)} รายการ"
                                    )
                                else:
                                    reply_message(
                                        reply_token,
                                        result.get("message", "ซิงก์คะแนนไม่สำเร็จ")
                                    )
                            except Exception as e:
                                reply_message(reply_token, f"ซิงก์คะแนนห้อง {classroom} ไม่สำเร็จ\n{str(e)}")
                        else:
                            rooms = get_teacher_rooms(user_id)
                            result = sync_all_scores_for_teacher(user_id, rooms)
                            if not result.get("rooms"):
                                reply_message(
                                    reply_token,
                                    result.get("message", "ซิงก์คะแนนไม่สำเร็จ")
                                    + f"\n\nuserId: {user_id or '-'}"
                                )
                            else:
                                text_lines = [
                                    "ซิงก์คะแนนทุกห้องเสร็จแล้ว",
                                    f"ห้องทั้งหมด: {len(result.get('rooms', []))}",
                                    f"อัปเดตรวม: {result.get('updated', 0)} รายการ",
                                    "ถ้าต้องการลดดีเลย์ ใช้ SS ตามด้วยเลขห้อง เช่น SS 401",
                                ]
                                if result.get("results"):
                                    room_summaries = [
                                        f"{r['classroom']}: {r.get('updated', 0)}"
                                        for r in result["results"]
                                    ]
                                    text_lines.append("รายห้อง: " + ", ".join(room_summaries))
                                if result.get("failed"):
                                    failed_rooms = [f"{r['classroom']} ({r['message']})" for r in result["failed"]]
                                    text_lines.append("ไม่สำเร็จ: " + ", ".join(failed_rooms))
                                reply_message(reply_token, "\n".join(text_lines))

                elif text.startswith("อัปเดตชีต") or text.startswith("อัพเดตชีต"):
                    if not teacher:
                        reply_message(reply_token, "คำสั่งนี้ใช้ได้เฉพาะครู")
                    else:
                        matched, classroom = parse_room_scoped_command(text, ["อัปเดตชีต", "อัพเดตชีต"])
                        if classroom:
                            try:
                                result = update_classroom_sheet_for_teacher(user_id, classroom)
                                reply_message(
                                    reply_token,
                                    result.get("message", "อัปเดตชีตไม่สำเร็จ")
                                )
                            except Exception as e:
                                reply_message(reply_token, f"อัปเดตชีตห้อง {classroom} ไม่สำเร็จ\n{str(e)}")
                        else:
                            reply_message(
                                reply_token,
                                "ใช้ UU เพื่ออัปเดตชีตทุกห้อง หรือใช้ UU 401 เพื่ออัปเดตทีละห้อง"
                            )

                elif text.startswith("ซิงก์คะแนน") or text.startswith("ซิ้งคะแนน") or text.startswith("sync คะแนน"):
                    if not teacher:
                        reply_message(reply_token, "คำสั่งนี้ใช้ได้เฉพาะครู")
                    else:
                        matched, classroom = parse_room_scoped_command(
                            text,
                            ["ซิงก์คะแนน", "ซิ้งคะแนน", "sync คะแนน"],
                        )
                        if classroom:
                            try:
                                result = sync_classroom_scores_for_teacher(user_id, classroom)
                                if result.get("success"):
                                    reply_message(
                                        reply_token,
                                        f"ซิงก์คะแนนห้อง {classroom} เสร็จแล้ว\n"
                                        f"อัปเดต: {result.get('updated', 0)} รายการ"
                                    )
                                else:
                                    reply_message(
                                        reply_token,
                                        result.get("message", "ซิงก์คะแนนไม่สำเร็จ")
                                    )
                            except Exception as e:
                                reply_message(reply_token, f"ซิงก์คะแนนห้อง {classroom} ไม่สำเร็จ\n{str(e)}")
                        else:
                            reply_message(
                                reply_token,
                                "ใช้ SS เพื่อซิงก์คะแนนทุกห้อง หรือใช้ SS 401 เพื่อซิงก์ทีละห้อง"
                            )

                else:
                    if teacher:
                        reply_message(
                            reply_token,
                            "คุณเป็นครูในระบบแล้ว\n\n"
                            "พิมพ์ 'เมนู' เพื่ออัปเดตเมนูครู"
                        )
                    elif student:
                        reply_message(
                            reply_token,
                            "บัญชีนี้เป็นนักเรียนในระบบ\n\n"
                            "กรุณาใช้งานผ่านกลุ่มห้องเรียน"
                        )
                    else:
                        update_student_rich_menu(user_id)
                        reply_message(
                            reply_token,
                            f"{student_register_prompt_text()}\n\n"
                            "ถ้าเป็นครู ให้พิมพ์ 'ตั้งค่าครู'\n"
                            "ถ้าเป็นนักเรียน ให้พิมพ์ 'ลงทะเบียน'"
                        )

    return "OK", 200


# =========================================================
# Main
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)




