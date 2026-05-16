import os
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import (
    CellFormat,
    Color,
    TextFormat,
    format_cell_range,
    set_column_width,
    set_frozen,
    Borders,
    Border,
    batch_updater
)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def get_google_client():
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
        raise Exception("ไม่พบ GOOGLE_SHEET_ID")

    client = get_google_client()
    return client.open_by_key(spreadsheet_id)


def get_or_create_worksheet(sheet, title, rows=100, cols=30):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


def read_records(sheet, worksheet_name):
    try:
        ws = sheet.worksheet(worksheet_name)
        return ws.get_all_records()
    except gspread.WorksheetNotFound:
        return []


def is_late(submit_time_text, due_time_text):
    if not submit_time_text or not due_time_text:
        return ""

    try:
        submit_time = datetime.strptime(submit_time_text, "%Y-%m-%d %H:%M:%S")
    except:
        return ""

    # รองรับ due_date แบบ 2026-05-20 23:59
    try:
        due_time = datetime.strptime(due_time_text, "%Y-%m-%d %H:%M")
    except:
        try:
            due_time = datetime.strptime(due_time_text, "%Y-%m-%d %H:%M:%S")
        except:
            return ""

    return "ใช่" if submit_time > due_time else "ไม่"


def update_classroom_report(classroom):
    sheet = get_sheet()

    users = read_records(sheet, "users")
    assignments = read_records(sheet, "assignments")
    submissions = read_records(sheet, "submissions")

    # นักเรียนในห้องนี้
    students = [
        u for u in users
        if str(u.get("classroom", "")).strip() == str(classroom).strip()
    ]

    # เรียงตามเลขที่
    def sort_key(u):
        try:
            return int(u.get("student_code", 9999))
        except:
            return 9999

    students.sort(key=sort_key)

    # งานของห้องนี้ + งาน ALL
    class_assignments = [
        a for a in assignments
        if str(a.get("classroom", "")).strip() in [str(classroom).strip(), "ALL"]
    ]

    # เรียงตาม assignment_id ถ้ามี
    def assignment_sort_key(a):
        try:
            return int(a.get("assignment_id", 9999))
        except:
            return 9999

    class_assignments.sort(key=assignment_sort_key)

    ws = get_or_create_worksheet(sheet, str(classroom), rows=200, cols=80)
    ws.clear()

    # =========================
    # สร้างหัวตาราง
    # =========================

    row1 = ["", "", "", "งาน"]
    row2 = ["", "", ""]

    row3 = ["ชื่อ", "เลขที่", "ID Line"]

    for i, assignment in enumerate(class_assignments, start=1):
        title = assignment.get("title", f"งาน {i}")

        row2.extend([title, "", ""])
        row3.extend(["ตรวจเวลา", "เลยกำหนด", "คะแนน"])

    values = [
        row1,
        row2,
        row3,
    ]

    # =========================
    # เติมข้อมูลนักเรียน
    # =========================

    for student in students:
        line_user_id = student.get("line_user_id", "")
        full_name = student.get("full_name", "")
        student_code = student.get("student_code", "")

        row = [
            full_name,
            student_code,
            line_user_id
        ]

        for assignment in class_assignments:
            title = assignment.get("title", "")
            due_date = assignment.get("due_date", "")

            matched = None

            for sub in submissions:
                if (
                    str(sub.get("line_user_id", "")).strip() == str(line_user_id).strip()
                    and str(sub.get("homework_title", "")).strip() == str(title).strip()
                ):
                    matched = sub
                    break

            if matched:
                submit_time = matched.get("timestamp", "")
                late = is_late(submit_time, due_date)
                score = ""
                row.extend([submit_time, late, score])
            else:
                row.extend(["ยังไม่ส่ง", "-", ""])

        values.append(row)

    ws.update("A1", values)

    # =========================
    # Merge หัวงาน
    # =========================

    # merge A1:C2 ว่างด้านซ้าย
    try:
        ws.merge_cells("A1:C2")
    except:
        pass

    # merge D1 ไปถึงคอลัมน์สุดท้าย เป็นคำว่า งาน
    total_cols = 3 + len(class_assignments) * 3

    if total_cols >= 4:
        start_col = 4
        end_col = total_cols
        ws.merge_cells(1, start_col, 1, end_col)

    # merge ชื่องานแต่ละงานใน row 2 ทีละ 3 คอลัมน์
    col = 4
    for assignment in class_assignments:
        ws.merge_cells(2, col, 2, col + 2)
        col += 3

    # =========================
    # จัด format
    # =========================

    header_format = CellFormat(
        backgroundColor=Color(0.90, 0.90, 0.90),
        textFormat=TextFormat(bold=True),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE"
    )

    assignment_odd = CellFormat(
        backgroundColor=Color(0.96, 0.86, 0.78),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE"
    )

    assignment_even = CellFormat(
        backgroundColor=Color(0.78, 0.88, 0.72),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE"
    )

    center_format = CellFormat(
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE"
    )

    with batch_updater(sheet) as batch:
        format_cell_range(ws, "A1:C3", header_format)
        format_cell_range(ws, "D1:ZZ3", header_format)
        format_cell_range(ws, "A4:ZZ200", center_format)

        col = 4
        for idx, assignment in enumerate(class_assignments):
            # แปลงเลขคอลัมน์เป็น A1 notation แบบง่าย
            start_letter = gspread.utils.rowcol_to_a1(1, col).replace("1", "")
            end_letter = gspread.utils.rowcol_to_a1(1, col + 2).replace("1", "")

            color_format = assignment_odd if idx % 2 == 0 else assignment_even
            format_cell_range(ws, f"{start_letter}2:{end_letter}200", color_format)

            col += 3

    set_frozen(ws, rows=3, cols=3)

    # ปรับความกว้างคอลัมน์
    set_column_width(ws, "A", 180)
    set_column_width(ws, "B", 80)
    set_column_width(ws, "C", 220)

    print(f"อัปเดตชีตห้อง {classroom} สำเร็จ")


def update_all_classroom_reports():
    sheet = get_sheet()
    users = read_records(sheet, "users")

    classrooms = sorted(list(set([
        str(u.get("classroom", "")).strip()
        for u in users
        if str(u.get("classroom", "")).strip()
    ])))

    for classroom in classrooms:
        update_classroom_report(classroom)


if __name__ == "__main__":
    update_all_classroom_reports()