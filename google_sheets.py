import os
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials


# =========================
# CONFIG
# =========================

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


# =========================
# GOOGLE CLIENT
# =========================

def get_credentials():
    """
    ใช้ได้ 2 แบบ:
    1. บน Render ใช้ GOOGLE_CREDENTIALS_JSON
    2. ในเครื่อง ถ้าไม่มี env จะใช้ credentials.json
    """

    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    if os.path.exists("credentials.json"):
        return Credentials.from_service_account_file(
            "credentials.json",
            scopes=SCOPES
        )

    raise FileNotFoundError(
        "ไม่พบ GOOGLE_CREDENTIALS_JSON และไม่พบไฟล์ credentials.json"
    )


def get_client():
    creds = get_credentials()
    return gspread.authorize(creds)


def get_sheet():
    if not GOOGLE_SHEET_ID:
        raise Exception("ไม่พบ GOOGLE_SHEET_ID ใน .env หรือ Render Environment")

    client = get_client()
    return client.open_by_key(GOOGLE_SHEET_ID)


def get_or_create_worksheet(spreadsheet, title, headers):
    """
    ถ้าไม่มีแท็บ ให้สร้างใหม่
    ถ้ามีแล้ว แต่แถวแรกว่าง ให้ใส่หัวตาราง
    """

    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=max(len(headers), 10)
        )
        ws.append_row(headers)
        return ws

    values = ws.get_all_values()

    if not values:
        ws.append_row(headers)

    return ws


# =========================
# APPEND USERS
# =========================

def append_user(timestamp, line_user_id, full_name, student_code, classroom, role):
    """
    บันทึกข้อมูลลงทะเบียนลงแท็บ users
    """

    try:
        spreadsheet = get_sheet()

        headers = [
            "timestamp",
            "line_user_id",
            "full_name",
            "student_code",
            "classroom",
            "role"
        ]

        ws = get_or_create_worksheet(
            spreadsheet,
            "users",
            headers
        )

        ws.append_row([
            timestamp,
            line_user_id,
            full_name,
            student_code,
            classroom,
            role
        ])

        print("Google Sheets append user success:", full_name)
        return True

    except Exception as e:
        print("Google Sheets append error [users]:", e)
        return False


# =========================
# APPEND SUBMISSIONS
# =========================

def append_submission(
    timestamp,
    line_user_id,
    full_name,
    student_code,
    classroom,
    homework_title,
    message_type,
    line_message_id,
    file_name
):
    """
    บันทึกการส่งงานลงแท็บ submissions
    """

    try:
        spreadsheet = get_sheet()

        headers = [
            "timestamp",
            "line_user_id",
            "full_name",
            "student_code",
            "classroom",
            "homework_title",
            "message_type",
            "line_message_id",
            "file_name"
        ]

        ws = get_or_create_worksheet(
            spreadsheet,
            "submissions",
            headers
        )

        ws.append_row([
            timestamp,
            line_user_id,
            full_name,
            student_code,
            classroom,
            homework_title,
            message_type,
            line_message_id,
            file_name
        ])

        print("Google Sheets append submission success:", homework_title)
        return True

    except Exception as e:
        print("Google Sheets append error [submissions]:", e)
        return False


# =========================
# ASSIGNMENTS
# =========================

def get_pending_assignments(line_user_id, classroom):
    """
    อ่านงานจาก assignments แล้วเทียบกับ submissions
    """

    try:
        spreadsheet = get_sheet()

        try:
            assignments_ws = spreadsheet.worksheet("assignments")
        except gspread.WorksheetNotFound:
            assignments_ws = spreadsheet.add_worksheet(
                title="assignments",
                rows=1000,
                cols=10
            )
            assignments_ws.append_row([
                "assignment_id",
                "classroom",
                "title",
                "due_date",
                "max_score",
                "created_at"
            ])
            return []

        try:
            submissions_ws = spreadsheet.worksheet("submissions")
        except gspread.WorksheetNotFound:
            return []

        assignments = assignments_ws.get_all_records()
        submissions = submissions_ws.get_all_records()

        submitted_titles = set()

        for row in submissions:
            if str(row.get("line_user_id", "")).strip() == str(line_user_id).strip():
                submitted_titles.add(str(row.get("homework_title", "")).strip())

        pending = []

        for row in assignments:
            target_classroom = str(row.get("classroom", "")).strip()
            title = str(row.get("title", "")).strip()

            if not title:
                continue

            if target_classroom not in [str(classroom), "ALL", "all", ""]:
                continue

            if title not in submitted_titles:
                pending.append(row)

        return pending

    except Exception as e:
        print("Google Sheets read error [assignments]:", e)
        return []


# =========================
# ANNOUNCEMENTS
# =========================

def get_latest_announcements(limit=5):
    """
    อ่านประกาศจากแท็บ announcements
    """

    try:
        spreadsheet = get_sheet()

        try:
            ws = spreadsheet.worksheet("announcements")
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(
                title="announcements",
                rows=1000,
                cols=10
            )
            ws.append_row([
                "created_at",
                "title",
                "body"
            ])
            return []

        records = ws.get_all_records()

        records = list(reversed(records))

        return records[:limit]

    except Exception as e:
        print("Google Sheets read error [announcements]:", e)
        return []