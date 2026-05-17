import os
import json
import uuid
import mimetypes
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify, render_template

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from io import BytesIO


# =========================================================
# Flask
# =========================================================

app = Flask(__name__)


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
# ENV
# =========================================================

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")

CRON_SECRET = os.getenv("CRON_SECRET", "")
TEACHER_SETUP_CODE = os.getenv("TEACHER_SETUP_CODE", "")

# LIFF นักเรียน
LIFF_STUDENT_REGISTER_ID = os.getenv("LIFF_STUDENT_REGISTER_ID", "")
LIFF_STUDENT_SUBMIT_ID = os.getenv("LIFF_STUDENT_SUBMIT_ID", "")
LIFF_STUDENT_PENDING_ID = os.getenv("LIFF_STUDENT_PENDING_ID", "")
LIFF_STUDENT_QUESTION_ID = os.getenv("LIFF_STUDENT_QUESTION_ID", "")
LIFF_STUDENT_ANNOUNCE_ID = os.getenv("LIFF_STUDENT_ANNOUNCE_ID", "")

# LIFF ครู
LIFF_TEACHER_SETUP_ID = os.getenv("LIFF_TEACHER_SETUP_ID", "")
LIFF_TEACHER_ASSIGNMENT_ID = os.getenv("LIFF_TEACHER_ASSIGNMENT_ID", "")
LIFF_TEACHER_PENDING_ID = os.getenv("LIFF_TEACHER_PENDING_ID", "")
LIFF_TEACHER_QUESTIONS_ID = os.getenv("LIFF_TEACHER_QUESTIONS_ID", "")
LIFF_TEACHER_ANNOUNCE_ID = os.getenv("LIFF_TEACHER_ANNOUNCE_ID", "")

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
    รองรับ 2 แบบ:
    1. GOOGLE_SERVICE_ACCOUNT_JSON เป็น JSON string ใน Render
    2. credentials.json อยู่ในโปรเจกต์
    """
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    if service_account_json:
        info = json.loads(service_account_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)


def get_gspread_client():
    creds = get_google_credentials()
    return gspread.authorize(creds)


def get_spreadsheet():
    gc = get_gspread_client()
    return gc.open_by_key(GOOGLE_SHEET_ID)


def get_worksheet(sheet_name):
    sh = get_spreadsheet()
    try:
        return sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=50)
        return ws


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


def push_message(to, text):
    if not to:
        return

    payload = {
        "to": to,
        "messages": [
            {
                "type": "text",
                "text": text,
            }
        ],
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
        return

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
    except Exception as e:
        print("[link_rich_menu_to_user] Error:", e)


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
        "title",
        "description",
        "start_date",
        "due_date",
        "max_score",
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
        "status",
        "answer_text",
        "answered_at",
        "answered_by",
        "student_seen",
    ],
    "announcements": [
        "announcement_id",
        "created_at",
        "teacher_line_user_id",
        "teacher_name",
        "classroom",
        "message",
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
        "message",
    ],
}


def setup_base_sheets():
    sh = get_spreadsheet()

    existing_titles = [ws.title for ws in sh.worksheets()]

    for sheet_name, headers in BASE_SHEETS.items():
        if sheet_name in existing_titles:
            ws = sh.worksheet(sheet_name)
        else:
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=max(20, len(headers) + 5))

        current_headers = ws.row_values(1)
        if not current_headers:
            ws.append_row(headers)
        else:
            # เพิ่ม header ที่ยังไม่มี
            changed = False
            for h in headers:
                if h not in current_headers:
                    current_headers.append(h)
                    changed = True
            if changed:
                ws.update("1:1", [current_headers])


def ensure_headers(ws, headers):
    current = ws.row_values(1)
    if not current:
        ws.append_row(headers)
        return headers

    changed = False
    for h in headers:
        if h not in current:
            current.append(h)
            changed = True

    if changed:
        ws.update("1:1", [current])

    return current


# =========================================================
# Helpers: Records
# =========================================================

def find_record_by_value(sheet_name, key, value):
    ws = get_worksheet(sheet_name)
    records = ws.get_all_records()

    for i, r in enumerate(records, start=2):
        if str(r.get(key, "")).strip() == str(value).strip():
            return i, r

    return None, None


def get_student_by_line_user_id(user_id):
    _, r = find_record_by_value("students", "student_line_user_id", user_id)
    return r


def get_teacher_by_line_user_id(user_id):
    _, r = find_record_by_value("teachers", "teacher_line_user_id", user_id)
    return r


def get_teacher_rooms(user_id):
    teacher = get_teacher_by_line_user_id(user_id)
    if not teacher:
        return []

    rooms_text = str(teacher.get("rooms", "")).strip()
    if not rooms_text:
        return []

    return [r.strip() for r in rooms_text.split(",") if r.strip()]


def get_class_group_id(classroom):
    ws = get_worksheet("class_groups")
    records = ws.get_all_records()

    for r in records:
        if str(r.get("classroom", "")).strip() == str(classroom).strip():
            return str(r.get("group_id", "")).strip()

    return ""


def upsert_class_group(classroom, group_id):
    ws = get_worksheet("class_groups")
    ensure_headers(ws, BASE_SHEETS["class_groups"])

    records = ws.get_all_records()
    headers = ws.row_values(1)

    for i, r in enumerate(records, start=2):
        if str(r.get("classroom", "")).strip() == str(classroom).strip():
            ws.update_cell(i, headers.index("group_id") + 1, group_id)
            ws.update_cell(i, headers.index("updated_at") + 1, now_text())
            return

    ws.append_row([
        classroom,
        group_id,
        now_text(),
    ])


# =========================================================
# Classroom Sheet
# =========================================================

def classroom_sheet_name(classroom):
    return f"ห้อง_{classroom}"


def create_or_update_classroom_sheet(classroom):
    sheet_name = classroom_sheet_name(classroom)
    ws = get_worksheet(sheet_name)

    headers = [
        "student_code",
        "student_name",
        "student_line_user_id",
    ]

    ensure_headers(ws, headers)
    return ws


def add_student_to_classroom_sheet(student_code, student_name, student_line_user_id, classroom):
    ws = create_or_update_classroom_sheet(classroom)
    records = ws.get_all_records()
    headers = ws.row_values(1)

    for i, r in enumerate(records, start=2):
        if str(r.get("student_line_user_id", "")).strip() == student_line_user_id:
            ws.update_cell(i, headers.index("student_code") + 1, student_code)
            ws.update_cell(i, headers.index("student_name") + 1, student_name)
            return

    row = [""] * len(headers)
    row[headers.index("student_code")] = student_code
    row[headers.index("student_name")] = student_name
    row[headers.index("student_line_user_id")] = student_line_user_id
    ws.append_row(row)


def add_assignment_header_to_classroom_sheet(classroom, assignment_title):
    ws = create_or_update_classroom_sheet(classroom)
    headers = ws.row_values(1)

    if assignment_title not in headers:
        headers.append(assignment_title)
        ws.update("1:1", [headers])


def mark_submission_in_classroom_sheet(classroom, student_line_user_id, assignment_title, file_url):
    ws = create_or_update_classroom_sheet(classroom)
    headers = ws.row_values(1)

    if assignment_title not in headers:
        headers.append(assignment_title)
        ws.update("1:1", [headers])

    records = ws.get_all_records()
    headers = ws.row_values(1)

    assignment_col = headers.index(assignment_title) + 1

    for i, r in enumerate(records, start=2):
        if str(r.get("student_line_user_id", "")).strip() == student_line_user_id:
            ws.update_cell(i, assignment_col, file_url)
            return True

    return False


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
        if aid and aid not in submitted_ids:
            return True

    return False


def student_has_unseen_answer(student_line_user_id):
    ws = get_worksheet("questions")
    records = ws.get_all_records()

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
        return

    student = get_student_by_line_user_id(student_line_user_id)

    if not student:
        if STUDENT_RICH_MENU_REGISTER_ID:
            link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_REGISTER_ID)
        return

    has_pending = student_has_pending_work(student_line_user_id)
    has_answer = student_has_unseen_answer(student_line_user_id)

    if has_pending and has_answer and STUDENT_RICH_MENU_BOTH_ALERT_ID:
        link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_BOTH_ALERT_ID)
    elif has_pending and STUDENT_RICH_MENU_PENDING_ALERT_ID:
        link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_PENDING_ALERT_ID)
    elif has_answer and STUDENT_RICH_MENU_ANSWER_ALERT_ID:
        link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_ANSWER_ALERT_ID)
    elif STUDENT_RICH_MENU_NORMAL_ID:
        link_rich_menu_to_user(student_line_user_id, STUDENT_RICH_MENU_NORMAL_ID)


def teacher_has_pending_questions(teacher_line_user_id):
    rooms = get_teacher_rooms(teacher_line_user_id)
    if not rooms:
        return False

    ws = get_worksheet("questions")
    records = ws.get_all_records()

    for r in records:
        classroom = str(r.get("classroom", "")).strip()
        status = str(r.get("status", "")).strip()
        if classroom in rooms and status == "pending":
            return True

    return False


def update_teacher_rich_menu(teacher_line_user_id):
    if not teacher_line_user_id:
        return

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)

    if not teacher:
        if TEACHER_RICH_MENU_SETUP_ID:
            link_rich_menu_to_user(teacher_line_user_id, TEACHER_RICH_MENU_SETUP_ID)
        return

    if teacher_has_pending_questions(teacher_line_user_id) and TEACHER_RICH_MENU_QUESTION_ALERT_ID:
        link_rich_menu_to_user(teacher_line_user_id, TEACHER_RICH_MENU_QUESTION_ALERT_ID)
    elif TEACHER_RICH_MENU_NORMAL_ID:
        link_rich_menu_to_user(teacher_line_user_id, TEACHER_RICH_MENU_NORMAL_ID)


# =========================================================
# Assignment / Submission Helpers
# =========================================================

def get_assignments_by_classroom(classroom):
    ws = get_worksheet("assignments")
    records = ws.get_all_records()

    result = []
    for r in records:
        if str(r.get("classroom", "")).strip() == str(classroom).strip():
            result.append(r)

    return result


def get_assignment_by_id(assignment_id):
    ws = get_worksheet("assignments")
    records = ws.get_all_records()

    for r in records:
        if str(r.get("assignment_id", "")).strip() == str(assignment_id).strip():
            return r

    return None


def get_submissions_by_student(student_line_user_id):
    ws = get_worksheet("submissions")
    records = ws.get_all_records()

    return [
        r for r in records
        if str(r.get("student_line_user_id", "")).strip() == str(student_line_user_id).strip()
    ]


def get_submission(student_line_user_id, assignment_id):
    ws = get_worksheet("submissions")
    records = ws.get_all_records()

    for r in records:
        if (
            str(r.get("student_line_user_id", "")).strip() == str(student_line_user_id).strip()
            and str(r.get("assignment_id", "")).strip() == str(assignment_id).strip()
        ):
            return r

    return None


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


def is_late_submission(due_date_text, submitted_at_text=None):
    due = parse_date(due_date_text)
    if not due:
        return "ไม่ทราบ"

    if submitted_at_text:
        try:
            submitted_dt = datetime.strptime(submitted_at_text, "%Y-%m-%d %H:%M:%S")
            submitted_date = submitted_dt.date()
        except Exception:
            submitted_date = today_date()
    else:
        submitted_date = today_date()

    return "ใช่" if submitted_date > due else "ไม่ใช่"


# =========================================================
# Google Drive Helpers
# =========================================================

def find_or_create_drive_folder(folder_name, parent_id):
    service = get_drive_service()

    query = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{folder_name}' "
        f"and '{parent_id}' in parents "
        f"and trashed=false"
    )

    res = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
    ).execute()

    files = res.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    folder = service.files().create(
        body=metadata,
        fields="id",
    ).execute()

    return folder["id"]


def upload_file_to_drive(file_bytes, file_name, classroom, assignment_title):
    service = get_drive_service()

    room_folder_id = find_or_create_drive_folder(
        f"ห้อง_{classroom}",
        GOOGLE_DRIVE_ROOT_FOLDER_ID,
    )

    assignment_folder_id = find_or_create_drive_folder(
        f"งาน_{assignment_title}",
        room_folder_id,
    )

    mime_type, _ = mimetypes.guess_type(file_name)
    if not mime_type:
        mime_type = "application/octet-stream"

    media = MediaIoBaseUpload(
        BytesIO(file_bytes),
        mimetype=mime_type,
        resumable=False,
    )

    metadata = {
        "name": file_name,
        "parents": [assignment_folder_id],
    }

    uploaded = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    file_id = uploaded["id"]

    # ทำให้คนมีลิงก์เปิดดูได้
    try:
        service.permissions().create(
            fileId=file_id,
            body={
                "type": "anyone",
                "role": "reader",
            },
        ).execute()
    except Exception as e:
        print("[drive permission] Error:", e)

    file = service.files().get(
        fileId=file_id,
        fields="id, webViewLink",
    ).execute()

    return file.get("webViewLink", "")


# =========================================================
# Pages
# =========================================================

@app.route("/")
def index():
    return "LINE School Bot is running."


# ---------- Student Pages ----------

@app.route("/student-register")
def student_register_page():
    return render_template(
        "student_register.html",
        liff_id=LIFF_STUDENT_REGISTER_ID,
    )


@app.route("/student-submit")
def student_submit_page():
    return render_template(
        "student_submit.html",
        liff_id=LIFF_STUDENT_SUBMIT_ID,
    )


@app.route("/student-pending")
def student_pending_page():
    return render_template(
        "student_pending.html",
        liff_id=LIFF_STUDENT_PENDING_ID,
    )


@app.route("/student-question")
def student_question_page():
    return render_template(
        "student_question.html",
        liff_id=LIFF_STUDENT_QUESTION_ID,
    )


@app.route("/student-announce")
def student_announce_page():
    return render_template(
        "student_announce.html",
        liff_id=LIFF_STUDENT_ANNOUNCE_ID,
    )


# ---------- Teacher Pages ----------

@app.route("/teacher-setup")
def teacher_setup_page():
    return render_template(
        "teacher_setup.html",
        liff_id=LIFF_TEACHER_SETUP_ID,
    )


@app.route("/teacher-assignment")
def teacher_assignment_page():
    return render_template(
        "teacher_assignment.html",
        liff_id=LIFF_TEACHER_ASSIGNMENT_ID,
    )


@app.route("/teacher-pending")
def teacher_pending_page():
    return render_template(
        "teacher_pending.html",
        liff_id=LIFF_TEACHER_PENDING_ID,
    )


@app.route("/teacher-questions")
def teacher_questions_page():
    return render_template(
        "teacher_questions.html",
        liff_id=LIFF_TEACHER_QUESTIONS_ID,
    )


@app.route("/teacher-announce")
def teacher_announce_page():
    return render_template(
        "teacher_announce.html",
        liff_id=LIFF_TEACHER_ANNOUNCE_ID,
    )


# =========================================================
# Debug
# =========================================================

@app.route("/debug/env")
def debug_env():
    return jsonify({
        "LINE_CHANNEL_ACCESS_TOKEN": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "LINE_CHANNEL_SECRET": bool(LINE_CHANNEL_SECRET),
        "GOOGLE_SHEET_ID": bool(GOOGLE_SHEET_ID),
        "GOOGLE_DRIVE_ROOT_FOLDER_ID": bool(GOOGLE_DRIVE_ROOT_FOLDER_ID),
        "CRON_SECRET": bool(CRON_SECRET),
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


@app.route("/debug/setup-sheets")
def debug_setup_sheets():
    setup_base_sheets()
    return jsonify({
        "success": True,
        "message": "setup sheets completed",
    })


@app.route("/debug/update-richmenu/<user_id>")
def debug_update_richmenu(user_id):
    teacher = get_teacher_by_line_user_id(user_id)
    student = get_student_by_line_user_id(user_id)

    if teacher:
        update_teacher_rich_menu(user_id)
        return jsonify({"success": True, "type": "teacher"})

    if student:
        update_student_rich_menu(user_id)
        return jsonify({"success": True, "type": "student"})

    unlink_rich_menu_from_user(user_id)
    return jsonify({"success": True, "type": "guest"})


# =========================================================
# API: Teacher
# =========================================================

@app.route("/api/teacher/setup", methods=["POST"])
def api_teacher_setup():
    try:
        data = request.get_json()

        teacher_line_user_id = str(data.get("teacher_line_user_id", "")).strip()
        teacher_name = str(data.get("teacher_name", "")).strip()
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

        room_list = [r.strip() for r in rooms.split(",") if r.strip()]
        clean_rooms = ",".join(room_list)

        if not room_list:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกห้องที่ดูแล เช่น 401,402",
            })

        ws = get_worksheet("teachers")
        ensure_headers(ws, BASE_SHEETS["teachers"])
        records = ws.get_all_records()
        headers = ws.row_values(1)

        for i, r in enumerate(records, start=2):
            if str(r.get("teacher_line_user_id", "")).strip() == teacher_line_user_id:
                ws.update_cell(i, headers.index("teacher_name") + 1, teacher_name)
                ws.update_cell(i, headers.index("rooms") + 1, clean_rooms)

                for room in room_list:
                    create_or_update_classroom_sheet(room)

                update_teacher_rich_menu(teacher_line_user_id)

                return jsonify({
                    "success": True,
                    "message": "อัปเดตข้อมูลครูเรียบร้อยแล้ว",
                })

        ws.append_row([
            teacher_line_user_id,
            teacher_name,
            clean_rooms,
            now_text(),
        ])

        for room in room_list:
            create_or_update_classroom_sheet(room)

        update_teacher_rich_menu(teacher_line_user_id)

        return jsonify({
            "success": True,
            "message": "บันทึกเรียบร้อยแล้ว สร้างแท็บห้องเรียบร้อยแล้ว",
        })

    except Exception as e:
        print("[api_teacher_setup] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/my-rooms")
def api_teacher_my_rooms():
    teacher_line_user_id = request.args.get("teacher_line_user_id", "").strip()

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

        teacher_line_user_id = str(data.get("teacher_line_user_id", "")).strip()
        classroom = str(data.get("classroom", "")).strip()
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        start_date = str(data.get("start_date", "")).strip()
        due_date = str(data.get("due_date", "")).strip()
        max_score = str(data.get("max_score", "")).strip()

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

        if not classroom or not title or not due_date:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกห้อง ชื่องาน และวันสิ้นสุด",
            })

        assignment_id = "as_" + uuid.uuid4().hex[:12]
        teacher_name = str(teacher.get("teacher_name", "")).strip()

        ws = get_worksheet("assignments")
        ensure_headers(ws, BASE_SHEETS["assignments"])

        ws.append_row([
            assignment_id,
            now_text(),
            teacher_line_user_id,
            teacher_name,
            classroom,
            title,
            description,
            start_date,
            due_date,
            max_score,
        ])

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
        })

    except Exception as e:
        print("[api_teacher_assignment] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/teacher/pending")
def api_teacher_pending():
    teacher_line_user_id = request.args.get("teacher_line_user_id", "").strip()
    classroom = request.args.get("classroom", "").strip()
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
    pending_students = []

    for s in students:
        sid = str(s.get("student_line_user_id", "")).strip()
        sub = get_submission(sid, assignment_id)
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


@app.route("/api/teacher/questions")
def api_teacher_questions():
    teacher_line_user_id = request.args.get("teacher_line_user_id", "").strip()

    teacher = get_teacher_by_line_user_id(teacher_line_user_id)
    if not teacher:
        return jsonify({
            "success": False,
            "message": "ไม่มีสิทธิ์ครู",
        })

    rooms = get_teacher_rooms(teacher_line_user_id)

    ws = get_worksheet("questions")
    records = ws.get_all_records()

    result = []
    for r in records:
        if (
            str(r.get("classroom", "")).strip() in rooms
            and str(r.get("status", "")).strip() == "pending"
        ):
            result.append(r)

    return jsonify({
        "success": True,
        "questions": result,
    })


@app.route("/api/teacher/answer-question", methods=["POST"])
def api_teacher_answer_question():
    try:
        data = request.get_json()

        teacher_line_user_id = str(data.get("teacher_line_user_id", "")).strip()
        question_id = str(data.get("question_id", "")).strip()
        answer_text = str(data.get("answer_text", "")).strip()

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
        records = ws.get_all_records()
        headers = ws.row_values(1)

        rooms = get_teacher_rooms(teacher_line_user_id)

        for i, r in enumerate(records, start=2):
            if str(r.get("question_id", "")).strip() == question_id:
                classroom = str(r.get("classroom", "")).strip()

                if classroom not in rooms:
                    return jsonify({
                        "success": False,
                        "message": "คุณไม่มีสิทธิ์ตอบคำถามของห้องนี้",
                    })

                ws.update_cell(i, headers.index("status") + 1, "answered")
                ws.update_cell(i, headers.index("answer_text") + 1, answer_text)
                ws.update_cell(i, headers.index("answered_at") + 1, now_text())
                ws.update_cell(i, headers.index("answered_by") + 1, teacher_line_user_id)
                ws.update_cell(i, headers.index("student_seen") + 1, "no")

                student_line_user_id = str(r.get("student_line_user_id", "")).strip()
                student_name = str(r.get("student_name", "")).strip()

                # ถ้านักเรียนแอดบอทไว้ จะ push ได้
                push_message(
                    student_line_user_id,
                    f"ครูตอบคำถามของคุณแล้ว\n\nคำตอบ:\n{answer_text}",
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

        teacher_line_user_id = str(data.get("teacher_line_user_id", "")).strip()
        classroom = str(data.get("classroom", "")).strip()
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

        teacher_name = str(teacher.get("teacher_name", "")).strip()
        announcement_id = "an_" + uuid.uuid4().hex[:12]

        ws = get_worksheet("announcements")
        ensure_headers(ws, BASE_SHEETS["announcements"])
        ws.append_row([
            announcement_id,
            now_text(),
            teacher_line_user_id,
            teacher_name,
            classroom,
            message,
        ])

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


# =========================================================
# API: Student
# =========================================================

def get_students_by_classroom(classroom):
    ws = get_worksheet("students")
    records = ws.get_all_records()

    return [
        r for r in records
        if str(r.get("classroom", "")).strip() == str(classroom).strip()
    ]


@app.route("/api/student/register", methods=["POST"])
def api_student_register():
    try:
        data = request.get_json()

        student_line_user_id = str(data.get("student_line_user_id", "")).strip()
        student_name = str(data.get("student_name", "")).strip()
        student_code = str(data.get("student_code", "")).strip()
        classroom = str(data.get("classroom", "")).strip()

        if not student_line_user_id or not student_name or not student_code or not classroom:
            return jsonify({
                "success": False,
                "message": "กรุณากรอกข้อมูลให้ครบ",
            })

        ws = get_worksheet("students")
        ensure_headers(ws, BASE_SHEETS["students"])
        records = ws.get_all_records()
        headers = ws.row_values(1)

        for i, r in enumerate(records, start=2):
            if str(r.get("student_line_user_id", "")).strip() == student_line_user_id:
                ws.update_cell(i, headers.index("student_name") + 1, student_name)
                ws.update_cell(i, headers.index("student_code") + 1, student_code)
                ws.update_cell(i, headers.index("classroom") + 1, classroom)

                add_student_to_classroom_sheet(
                    student_code,
                    student_name,
                    student_line_user_id,
                    classroom,
                )

                update_student_rich_menu(student_line_user_id)

                return jsonify({
                    "success": True,
                    "message": "อัปเดตข้อมูลนักเรียนเรียบร้อยแล้ว",
                })

        ws.append_row([
            student_line_user_id,
            student_name,
            student_code,
            classroom,
            now_text(),
        ])

        add_student_to_classroom_sheet(
            student_code,
            student_name,
            student_line_user_id,
            classroom,
        )

        update_student_rich_menu(student_line_user_id)

        return jsonify({
            "success": True,
            "message": "ลงทะเบียนเรียบร้อยแล้ว",
        })

    except Exception as e:
        print("[api_student_register] Error:", e)
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

    result = []
    for a in assignments:
        aid = str(a.get("assignment_id", "")).strip()
        item = dict(a)
        item["submitted"] = aid in submitted_ids
        result.append(item)

    return jsonify({
        "success": True,
        "student": student,
        "assignments": result,
    })


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
        if aid in submitted_ids:
            submitted.append(a)
        else:
            pending.append(a)

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
        student_line_user_id = request.form.get("student_line_user_id", "").strip()
        assignment_id = request.form.get("assignment_id", "").strip()
        note = request.form.get("note", "").strip()

        file = request.files.get("file")

        if not student_line_user_id or not assignment_id or not file:
            return jsonify({
                "success": False,
                "message": "กรุณาเลือกงานและอัปโหลดไฟล์",
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

        original_name = file.filename or "upload_file"
        safe_name = f"{student.get('student_code', '')}_{student.get('student_name', '')}_{assignment_title}_{original_name}"

        file_bytes = file.read()
        file_url = upload_file_to_drive(
            file_bytes=file_bytes,
            file_name=safe_name,
            classroom=classroom,
            assignment_title=assignment_title,
        )

        submitted_at = now_text()
        late = is_late_submission(due_date, submitted_at)

        submission_id = "sub_" + uuid.uuid4().hex[:12]

        ws = get_worksheet("submissions")
        ensure_headers(ws, BASE_SHEETS["submissions"])

        ws.append_row([
            submission_id,
            submitted_at,
            assignment_id,
            assignment_title,
            student_line_user_id,
            str(student.get("student_name", "")).strip(),
            str(student.get("student_code", "")).strip(),
            classroom,
            file_url,
            original_name,
            note,
            late,
            "",
            "",
            "",
            "",
        ])

        mark_submission_in_classroom_sheet(
            classroom,
            student_line_user_id,
            assignment_title,
            file_url,
        )

        update_student_rich_menu(student_line_user_id)

        return jsonify({
            "success": True,
            "message": "ส่งงานเรียบร้อยแล้ว",
            "file_url": file_url,
        })

    except Exception as e:
        print("[api_student_submit] Error:", e)
        return jsonify({
            "success": False,
            "message": str(e),
        })


@app.route("/api/student/question", methods=["POST"])
def api_student_question():
    try:
        data = request.get_json()

        student_line_user_id = str(data.get("student_line_user_id", "")).strip()
        question_text = str(data.get("question_text", "")).strip()

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

        ws = get_worksheet("questions")
        ensure_headers(ws, BASE_SHEETS["questions"])

        ws.append_row([
            question_id,
            now_text(),
            student_line_user_id,
            student_name,
            classroom,
            question_text,
            "pending",
            "",
            "",
            "",
            "",
        ])

        # อัปเดต rich menu ครูที่ดูแลห้องนี้
        teachers_ws = get_worksheet("teachers")
        teachers = teachers_ws.get_all_records()
        for t in teachers:
            rooms = [r.strip() for r in str(t.get("rooms", "")).split(",") if r.strip()]
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
    records = ws.get_all_records()
    headers = ws.row_values(1)

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
        for i in row_indexes_to_mark:
            ws.update_cell(i, col, "yes")

    update_student_rich_menu(student_line_user_id)

    return jsonify({
        "success": True,
        "questions": result,
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

    ws = get_worksheet("announcements")
    records = ws.get_all_records()

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
                            "uri": f"https://liff.line.me/{LIFF_STUDENT_REGISTER_ID}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "uri",
                            "label": "ส่งงาน",
                            "uri": f"https://liff.line.me/{LIFF_STUDENT_SUBMIT_ID}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "งานค้าง",
                            "uri": f"https://liff.line.me/{LIFF_STUDENT_PENDING_ID}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "ถามคำถาม",
                            "uri": f"https://liff.line.me/{LIFF_STUDENT_QUESTION_ID}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "uri",
                            "label": "ประกาศ",
                            "uri": f"https://liff.line.me/{LIFF_STUDENT_ANNOUNCE_ID}",
                        },
                    },
                ],
            },
        },
    }


# =========================================================
# Deadline Reminder
# =========================================================

def build_deadline_mention_message(classroom, assignment, pending_students):
    title = str(assignment.get("title", "")).strip()
    due_date = str(assignment.get("due_date", "")).strip()

    text = (
        f"แจ้งเตือนส่งงาน ห้อง {classroom}\n\n"
        f"งาน: {title}\n"
        f"กำหนดส่ง: {due_date}\n\n"
        f"นักเรียนที่ยังไม่ส่ง:\n"
    )

    mentionees = []
    index = len(text)

    names = []

    for s in pending_students:
        name = str(s.get("student_name", "")).strip()
        user_id = str(s.get("student_line_user_id", "")).strip()

        if not name:
            name = "นักเรียน"

        mention_text = f"@{name}"
        names.append(mention_text)

        if user_id:
            mentionees.append({
                "index": index,
                "length": len(mention_text),
                "userId": user_id,
            })

        text += mention_text + " "
        index = len(text)

    if not pending_students:
        text += "ไม่มี นักเรียนส่งครบแล้ว"

    message = {
        "type": "text",
        "text": text,
    }

    if mentionees:
        message["mention"] = {
            "mentionees": mentionees,
        }

    return message


def notify_deadline_for_assignment(assignment_id):
    assignment = get_assignment_by_id(assignment_id)
    if not assignment:
        return {
            "success": False,
            "message": "ไม่พบ assignment",
        }

    classroom = str(assignment.get("classroom", "")).strip()
    students = get_students_by_classroom(classroom)

    pending_students = []
    for s in students:
        sid = str(s.get("student_line_user_id", "")).strip()
        sub = get_submission(sid, assignment_id)
        if not sub:
            pending_students.append(s)

    group_id = get_class_group_id(classroom)
    if not group_id:
        return {
            "success": False,
            "message": f"ยังไม่ได้ผูกกลุ่มของห้อง {classroom}",
        }

    message = build_deadline_mention_message(classroom, assignment, pending_students)
    res = push_messages(group_id, [message])

    # log
    try:
        ws = get_worksheet("deadline_logs")
        ensure_headers(ws, BASE_SHEETS["deadline_logs"])
        ws.append_row([
            "log_" + uuid.uuid4().hex[:12],
            now_text(),
            assignment_id,
            classroom,
            group_id,
            message.get("text", ""),
        ])
    except Exception as e:
        print("[deadline log] Error:", e)

    return {
        "success": True,
        "message": "sent",
        "classroom": classroom,
        "pending_count": len(pending_students),
        "line_status": res.status_code if res else None,
        "line_text": res.text if res else None,
    }


@app.route("/debug/notify-deadline")
def debug_notify_deadline():
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

    target_date = today_date() + timedelta(days=1)

    ws = get_worksheet("assignments")
    records = ws.get_all_records()

    results = []

    for a in records:
        due_date_text = str(a.get("due_date", "")).strip()
        due = parse_date(due_date_text)

        if not due:
            continue

        if due == target_date:
            assignment_id = str(a.get("assignment_id", "")).strip()
            if assignment_id:
                result = notify_deadline_for_assignment(assignment_id)
                results.append(result)

    return jsonify({
        "success": True,
        "target_date": target_date.strftime("%Y-%m-%d"),
        "count": len(results),
        "results": results,
    })


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
        group_id = source.get("groupId", "")

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
                # นักเรียนที่แอดส่วนตัว ให้ไม่มีเมนู และบอกให้ใช้ผ่านกลุ่ม
                unlink_rich_menu_from_user(user_id)
                reply_message(
                    reply_token,
                    "บัญชีนี้เป็นนักเรียนในระบบแล้ว\n\n"
                    "กรุณาใช้งานผ่านกลุ่มห้องเรียน"
                )

            else:
                # คนใหม่ที่แอดบอทเอง ไม่มีสิทธิ์อะไร
                unlink_rich_menu_from_user(user_id)
                reply_message(
                    reply_token,
                    "บัญชีนี้ยังไม่มีสิทธิ์ใช้งานครู\n\n"
                    "ถ้าคุณเป็นครู กรุณาเปิดหน้าตั้งค่าครูและกรอกรหัสครูที่ได้รับจากผู้ดูแล\n"
                    "ถ้าคุณเป็นนักเรียน กรุณาใช้งานผ่านกลุ่มห้องเรียน"
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

            # -----------------------------
            # คำสั่งในกลุ่ม
            # -----------------------------
            if source_type == "group":
                if text.startswith("ผูกห้อง"):
                    classroom = text.replace("ผูกห้อง", "").strip()

                    if not classroom:
                        reply_message(
                            reply_token,
                            "กรุณาพิมพ์รูปแบบ:\nผูกห้อง 401"
                        )
                    else:
                        upsert_class_group(classroom, group_id)
                        reply_message(
                            reply_token,
                            f"ผูกกลุ่มนี้กับห้อง {classroom} เรียบร้อยแล้ว"
                        )

                elif text in ["เมนู", "เมนูนักเรียน", "menu"]:
                    flex = make_student_group_flex_menu()
                    reply_messages(reply_token, [flex])

                elif text.startswith("ทดสอบแจ้งเตือน"):
                    assignment_id = text.replace("ทดสอบแจ้งเตือน", "").strip()

                    if not assignment_id:
                        reply_message(
                            reply_token,
                            "กรุณาพิมพ์:\nทดสอบแจ้งเตือน as_xxxxx"
                        )
                    else:
                        result = notify_deadline_for_assignment(assignment_id)
                        reply_message(
                            reply_token,
                            f"ผลทดสอบแจ้งเตือน:\n{json.dumps(result, ensure_ascii=False)}"
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

                if text in ["เมนู", "menu"]:
                    if teacher:
                        update_teacher_rich_menu(user_id)
                        reply_message(reply_token, "อัปเดตเมนูครูเรียบร้อยแล้ว")
                    elif student:
                        unlink_rich_menu_from_user(user_id)
                        reply_message(
                            reply_token,
                            "บัญชีนี้เป็นนักเรียนในระบบ\n\n"
                            "กรุณาใช้งานผ่านกลุ่มห้องเรียน"
                        )
                    else:
                        unlink_rich_menu_from_user(user_id)
                        reply_message(
                            reply_token,
                            "บัญชีนี้ยังไม่มีสิทธิ์ใช้งานครู\n\n"
                            "ถ้าเป็นครู กรุณาเปิดหน้า teacher-setup และกรอกรหัสครู\n"
                            "ถ้าเป็นนักเรียน กรุณาใช้งานผ่านกลุ่มห้องเรียน"
                        )

                elif text in ["ตั้งค่าครู", "ครู"]:
                    reply_message(
                        reply_token,
                        f"เปิดหน้าตั้งค่าครูผ่านลิงก์นี้:\nhttps://liff.line.me/{LIFF_TEACHER_SETUP_ID}\n\n"
                        "ต้องกรอกรหัสครูที่ได้รับจากผู้ดูแล"
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
                        reply_message(
                            reply_token,
                            "บัญชีนี้ยังไม่มีสิทธิ์ใช้งานครู\n\n"
                            "ถ้าเป็นครู ให้พิมพ์ 'ตั้งค่าครู'\n"
                            "ถ้าเป็นนักเรียน กรุณาใช้งานผ่านกลุ่มห้องเรียน"
                        )

    return "OK", 200


# =========================================================
# Main
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)