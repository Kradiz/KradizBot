import os
import json

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def get_google_client():
    """
    ใช้ได้ทั้งในเครื่องและบน Render

    ในเครื่อง:
    - ใช้ไฟล์ credentials.json

    บน Render:
    - ใช้ GOOGLE_CREDENTIALS_JSON จาก Environment
    """

    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if credentials_json:
        info = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            "credentials.json",
            scopes=SCOPES
        )

    return gspread.authorize(creds)


def get_sheet():
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")

    if not spreadsheet_id:
        raise Exception("ไม่พบ GOOGLE_SHEET_ID ใน .env หรือ Render Environment")

    client = get_google_client()
    return client.open_by_key(spreadsheet_id)


def get_or_create_worksheet(sheet, worksheet_name, rows=1000, cols=30):
    try:
        return sheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(
            title=worksheet_name,
            rows=rows,
            cols=cols
        )


def safe_append_row(worksheet_name, row):
    """
    เขียนข้อมูลลง Google Sheet
    ถ้า error จะ print ออกมา แต่ไม่ทำให้บอทล้ม
    """
    try:
        sheet = get_sheet()
        ws = get_or_create_worksheet(sheet, worksheet_name)
        ws.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"Google Sheets append error [{worksheet_name}]:", e)
        return False


def safe_get_records(worksheet_name):
    """
    อ่านข้อมูลจาก Google Sheet
    """
    try:
        sheet = get_sheet()
        ws = get_or_create_worksheet(sheet, worksheet_name)
        return ws.get_all_records()
    except Exception as e:
        print(f"Google Sheets read error [{worksheet_name}]:", e)
        return []


def append_user(timestamp, line_user_id, full_name, student_code, classroom, role="student"):
    return safe_append_row("users", [
        timestamp,
        line_user_id,
        full_name,
        student_code,
        classroom,
        role
    ])


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
    return safe_append_row("submissions", [
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


def get_users():
    return safe_get_records("users")


def get_submissions():
    return safe_get_records("submissions")


def get_assignments():
    return safe_get_records("assignments")


def get_announcements():
    return safe_get_records("announcements")


def get_assignments_for_classroom(classroom):
    assignments = get_assignments()
    result = []

    for item in assignments:
        item_classroom = str(item.get("classroom", "")).strip()

        if item_classroom == str(classroom).strip() or item_classroom.upper() == "ALL":
            result.append(item)

    def sort_key(x):
        try:
            return int(x.get("assignment_id", 999999))
        except:
            return 999999

    result.sort(key=sort_key)
    return result


def get_submissions_by_user(line_user_id):
    submissions = get_submissions()
    result = []

    for item in submissions:
        if str(item.get("line_user_id", "")).strip() == str(line_user_id).strip():
            result.append(item)

    return result


def get_pending_assignments(line_user_id, classroom):
    assignments = get_assignments_for_classroom(classroom)
    submissions = get_submissions_by_user(line_user_id)

    submitted_titles = set()

    for sub in submissions:
        submitted_titles.add(str(sub.get("homework_title", "")).strip())

    pending = []

    for assignment in assignments:
        title = str(assignment.get("title", "")).strip()

        if title and title not in submitted_titles:
            pending.append(assignment)

    return pending


def get_latest_announcements(limit=5):
    announcements = get_announcements()

    # เอาประกาศล่าสุดจากแถวล่างสุดขึ้นมาก่อน
    announcements = list(reversed(announcements))

    return announcements[:limit]