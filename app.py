import os
import uuid
from datetime import datetime

from flask import Flask, request, abort, render_template, jsonify
from dotenv import load_dotenv

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================
# Load ENV
# =========================

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

LIFF_TEACHER_SETUP_ID = os.getenv("LIFF_TEACHER_SETUP_ID")
LIFF_TEACHER_ASSIGNMENT_ID = os.getenv("LIFF_TEACHER_ASSIGNMENT_ID")
LIFF_TEACHER_PENDING_ID = os.getenv("LIFF_TEACHER_PENDING_ID")
LIFF_TEACHER_QUESTIONS_ID = os.getenv("LIFF_TEACHER_QUESTIONS_ID")
LIFF_TEACHER_ANNOUNCE_ID = os.getenv("LIFF_TEACHER_ANNOUNCE_ID")

if not CHANNEL_ACCESS_TOKEN:
    raise ValueError("ไม่พบ CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET:
    raise ValueError("ไม่พบ CHANNEL_SECRET")

if not GOOGLE_SHEET_ID:
    raise ValueError("ไม่พบ GOOGLE_SHEET_ID")


# =========================
# Flask / LINE
# =========================

app = Flask(__name__)

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# =========================
# Google Sheets
# =========================

def get_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=scopes
    )

    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def get_or_create_sheet(spreadsheet, title, headers, rows=200, cols=30):
    try:
        ws = spreadsheet.worksheet(title)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
        ws.append_row(headers)
    return ws


def ensure_main_sheets(spreadsheet):
    get_or_create_sheet(
        spreadsheet,
        "teachers",
        [
            "teacher_line_user_id",
            "teacher_name",
            "rooms",
            "created_at",
            "updated_at"
        ]
    )

    get_or_create_sheet(
        spreadsheet,
        "users",
        [
            "line_user_id",
            "role",
            "full_name",
            "student_code",
            "classroom",
            "created_at"
        ]
    )

    get_or_create_sheet(
        spreadsheet,
        "assignments",
        [
            "assignment_id",
            "classroom",
            "title",
            "description",
            "start_date",
            "due_date",
            "max_score",
            "teacher_line_user_id",
            "created_at"
        ]
    )

    get_or_create_sheet(
        spreadsheet,
        "submissions",
        [
            "submission_id",
            "assignment_id",
            "student_line_user_id",
            "student_name",
            "classroom",
            "submitted_at",
            "status",
            "score",
            "note"
        ]
    )

    get_or_create_sheet(
        spreadsheet,
        "questions",
        [
            "question_id",
            "created_at",
            "student_line_user_id",
            "student_name",
            "classroom",
            "question_text",
            "status",
            "answer_text",
            "answered_at",
            "teacher_line_user_id"
        ]
    )

    get_or_create_sheet(
        spreadsheet,
        "announcements",
        [
            "announcement_id",
            "created_at",
            "teacher_line_user_id",
            "teacher_name",
            "classroom",
            "message"
        ]
    )


# =========================
# Helpers
# =========================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def reply_text(reply_token, text):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=text)
    )


def parse_rooms(text):
    if not text:
        return []

    text = text.replace("，", ",")
    text = text.replace("、", ",")
    text = text.replace("\n", ",")
    text = text.replace(" ", ",")

    rooms = []
    for item in text.split(","):
        room = item.strip()
        if room and room not in rooms:
            rooms.append(room)

    return rooms


def safe_sheet_name(room):
    room = str(room).strip()
    room = room.replace("/", "-")
    room = room.replace("\\", "-")
    room = room.replace("?", "")
    room = room.replace("*", "")
    room = room.replace("[", "")
    room = room.replace("]", "")
    room = room.replace(":", "-")
    return f"ห้อง_{room}"


def get_teacher_rooms(spreadsheet, teacher_line_user_id):
    ws = get_or_create_sheet(
        spreadsheet,
        "teachers",
        [
            "teacher_line_user_id",
            "teacher_name",
            "rooms",
            "created_at",
            "updated_at"
        ]
    )

    records = ws.get_all_records()

    for row in records:
        if str(row.get("teacher_line_user_id")) == str(teacher_line_user_id):
            rooms_text = str(row.get("rooms", "")).strip()
            return parse_rooms(rooms_text)

    return []


def get_teacher_name(spreadsheet, teacher_line_user_id):
    ws = spreadsheet.worksheet("teachers")
    records = ws.get_all_records()

    for row in records:
        if str(row.get("teacher_line_user_id")) == str(teacher_line_user_id):
            return row.get("teacher_name", "")

    return ""


# =========================
# Classroom Sheet
# =========================

def create_classroom_sheet_if_not_exists(spreadsheet, room):
    sheet_name = safe_sheet_name(room)

    try:
        spreadsheet.worksheet(sheet_name)
        return sheet_name
    except WorksheetNotFound:
        pass

    ws = spreadsheet.add_worksheet(title=sheet_name, rows=120, cols=40)

    ws.update("A1:J1", [[f"สรุปงานห้อง {room}"]])
    ws.merge_cells("A1:J1")

    ws.update("A3:C3", [[
        "ชื่อ",
        "เลขที่",
        "ID Line"
    ]])

    fill_students_to_classroom_sheet(spreadsheet, room, ws)
    format_classroom_sheet(spreadsheet, ws)

    return sheet_name


def fill_students_to_classroom_sheet(spreadsheet, room, classroom_ws):
    users_ws = get_or_create_sheet(
        spreadsheet,
        "users",
        [
            "line_user_id",
            "role",
            "full_name",
            "student_code",
            "classroom",
            "created_at"
        ]
    )

    records = users_ws.get_all_records()
    values = []

    for row in records:
        classroom = str(row.get("classroom", "")).strip()

        if classroom == str(room).strip():
            values.append([
                row.get("full_name", ""),
                row.get("student_code", ""),
                row.get("line_user_id", "")
            ])

    if values:
        classroom_ws.update(f"A4:C{3 + len(values)}", values)


def format_classroom_sheet(spreadsheet, ws):
    sheet_id = ws.id

    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 10
                },
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "backgroundColor": {
                            "red": 0.89,
                            "green": 0.95,
                            "blue": 0.85
                        },
                        "textFormat": {
                            "bold": True,
                            "fontSize": 16
                        }
                    }
                },
                "fields": "userEnteredFormat"
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 2,
                    "endRowIndex": 3,
                    "startColumnIndex": 0,
                    "endColumnIndex": 40
                },
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "bold": True
                        }
                    }
                },
                "fields": "userEnteredFormat"
            }
        },
        {
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 60,
                    "startColumnIndex": 0,
                    "endColumnIndex": 20
                },
                "top": {"style": "SOLID", "width": 1},
                "bottom": {"style": "SOLID", "width": 1},
                "left": {"style": "SOLID", "width": 1},
                "right": {"style": "SOLID", "width": 1},
                "innerHorizontal": {"style": "SOLID", "width": 1},
                "innerVertical": {"style": "SOLID", "width": 1}
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": 3,
                        "frozenColumnCount": 3
                    }
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
            }
        }
    ]

    spreadsheet.batch_update({"requests": requests})


def add_assignment_header_to_classroom_sheet(spreadsheet, room, assignment_title):
    sheet_name = create_classroom_sheet_if_not_exists(spreadsheet, room)
    ws = spreadsheet.worksheet(sheet_name)

    row2 = ws.row_values(2)

    start_col = 4
    while True:
        value = ""
        if len(row2) >= start_col:
            value = row2[start_col - 1]

        if not value:
            break

        start_col += 3

    end_col = start_col + 2

    ws.update_cell(2, start_col, assignment_title)
    ws.merge_cells(
        start_row=2,
        start_col=start_col,
        end_row=2,
        end_col=end_col
    )

    ws.update(
        f"{gspread.utils.rowcol_to_a1(3, start_col)}:{gspread.utils.rowcol_to_a1(3, end_col)}",
        [["ครบเวลา", "เลยกำหนด", "คะแนน"]]
    )

    return sheet_name


# =========================
# Teacher Setup
# =========================

def save_teacher_rooms(spreadsheet, teacher_line_user_id, teacher_name, rooms):
    ws = spreadsheet.worksheet("teachers")
    records = ws.get_all_records()

    now = now_text()
    rooms_text = ",".join(rooms)

    found_row = None
    old_created_at = now

    for i, row in enumerate(records, start=2):
        if str(row.get("teacher_line_user_id")) == str(teacher_line_user_id):
            found_row = i
            old_created_at = row.get("created_at") or now
            break

    if found_row:
        ws.update(
            f"A{found_row}:E{found_row}",
            [[
                teacher_line_user_id,
                teacher_name,
                rooms_text,
                old_created_at,
                now
            ]]
        )
    else:
        ws.append_row([
            teacher_line_user_id,
            teacher_name,
            rooms_text,
            now,
            now
        ])


# =========================
# Web Pages
# =========================

@app.route("/")
def home():
    return "LINE School Bot is running"


@app.route("/teacher-setup")
def teacher_setup_page():
    return render_template(
        "teacher_setup.html",
        liff_id=LIFF_TEACHER_SETUP_ID
    )


@app.route("/teacher-assignment")
def teacher_assignment_page():
    return render_template(
        "teacher_assignment.html",
        liff_id=LIFF_TEACHER_ASSIGNMENT_ID
    )


@app.route("/teacher-pending")
def teacher_pending_page():
    return render_template(
        "teacher_pending.html",
        liff_id=LIFF_TEACHER_PENDING_ID
    )


@app.route("/teacher-questions")
def teacher_questions_page():
    return render_template(
        "teacher_questions.html",
        liff_id=LIFF_TEACHER_QUESTIONS_ID
    )


@app.route("/teacher-announce")
def teacher_announce_page():
    return render_template(
        "teacher_announce.html",
        liff_id=LIFF_TEACHER_ANNOUNCE_ID
    )


# =========================
# APIs
# =========================

@app.route("/api/teacher/setup", methods=["POST"])
def api_teacher_setup():
    try:
        data = request.get_json()

        teacher_line_user_id = data.get("teacher_line_user_id", "").strip()
        teacher_name = data.get("teacher_name", "").strip()
        rooms_text = data.get("rooms", "").strip()

        if not teacher_line_user_id:
            return jsonify(success=False, message="ไม่พบ LINE userId"), 400

        if not teacher_name:
            return jsonify(success=False, message="กรุณากรอกชื่อครู"), 400

        rooms = parse_rooms(rooms_text)

        if not rooms:
            return jsonify(success=False, message="กรุณากรอกห้องที่ดูแล"), 400

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        save_teacher_rooms(
            spreadsheet,
            teacher_line_user_id,
            teacher_name,
            rooms
        )

        created_sheets = []
        for room in rooms:
            created_sheets.append(
                create_classroom_sheet_if_not_exists(spreadsheet, room)
            )

        return jsonify(
            success=True,
            message="บันทึกเรียบร้อยแล้ว",
            rooms=rooms,
            sheets=created_sheets
        )

    except Exception as e:
        print("api_teacher_setup error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/rooms", methods=["GET"])
def api_teacher_rooms():
    try:
        teacher_line_user_id = request.args.get("teacher_line_user_id", "").strip()

        if not teacher_line_user_id:
            return jsonify(success=False, message="ไม่พบ LINE userId"), 400

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        rooms = get_teacher_rooms(spreadsheet, teacher_line_user_id)

        return jsonify(success=True, rooms=rooms)

    except Exception as e:
        print("api_teacher_rooms error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/assignment", methods=["POST"])
def api_teacher_assignment():
    try:
        data = request.get_json()

        teacher_line_user_id = data.get("teacher_line_user_id", "").strip()
        classroom = data.get("classroom", "").strip()
        title = data.get("title", "").strip()
        description = data.get("description", "").strip()
        start_date = data.get("start_date", "").strip()
        due_date = data.get("due_date", "").strip()
        max_score = data.get("max_score", "").strip()

        if not teacher_line_user_id:
            return jsonify(success=False, message="ไม่พบ LINE userId"), 400

        if not classroom:
            return jsonify(success=False, message="กรุณาเลือกห้อง"), 400

        if not title:
            return jsonify(success=False, message="กรุณากรอกชื่องาน"), 400

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        assignment_id = "A" + uuid.uuid4().hex[:10]

        ws = spreadsheet.worksheet("assignments")
        ws.append_row([
            assignment_id,
            classroom,
            title,
            description,
            start_date,
            due_date,
            max_score,
            teacher_line_user_id,
            now_text()
        ])

        add_assignment_header_to_classroom_sheet(
            spreadsheet,
            classroom,
            title
        )

        return jsonify(
            success=True,
            message="บันทึกงานเรียบร้อยแล้ว",
            assignment_id=assignment_id
        )

    except Exception as e:
        print("api_teacher_assignment error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/assignments", methods=["GET"])
def api_teacher_assignments():
    try:
        classroom = request.args.get("classroom", "").strip()

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        ws = spreadsheet.worksheet("assignments")
        records = ws.get_all_records()

        result = []

        for row in records:
            if classroom and str(row.get("classroom")) != classroom:
                continue

            result.append({
                "assignment_id": row.get("assignment_id"),
                "classroom": row.get("classroom"),
                "title": row.get("title"),
                "due_date": row.get("due_date"),
                "max_score": row.get("max_score")
            })

        return jsonify(success=True, assignments=result)

    except Exception as e:
        print("api_teacher_assignments error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/pending", methods=["GET"])
def api_teacher_pending():
    try:
        classroom = request.args.get("classroom", "").strip()
        assignment_id = request.args.get("assignment_id", "").strip()

        if not classroom:
            return jsonify(success=False, message="กรุณาเลือกห้อง"), 400

        if not assignment_id:
            return jsonify(success=False, message="กรุณาเลือกงาน"), 400

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        users_ws = spreadsheet.worksheet("users")
        submissions_ws = spreadsheet.worksheet("submissions")

        users = users_ws.get_all_records()
        submissions = submissions_ws.get_all_records()

        submitted_user_ids = set()

        for sub in submissions:
            if str(sub.get("assignment_id")) == assignment_id:
                submitted_user_ids.add(str(sub.get("student_line_user_id")))

        pending_students = []

        for user in users:
            if str(user.get("classroom")) != classroom:
                continue

            if str(user.get("role")) != "student":
                continue

            line_user_id = str(user.get("line_user_id"))

            if line_user_id not in submitted_user_ids:
                pending_students.append({
                    "student_name": user.get("full_name"),
                    "student_code": user.get("student_code"),
                    "line_user_id": line_user_id
                })

        return jsonify(success=True, students=pending_students)

    except Exception as e:
        print("api_teacher_pending error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/questions", methods=["GET"])
def api_teacher_questions():
    try:
        teacher_line_user_id = request.args.get("teacher_line_user_id", "").strip()

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        rooms = get_teacher_rooms(spreadsheet, teacher_line_user_id)

        ws = spreadsheet.worksheet("questions")
        records = ws.get_all_records()

        questions = []

        for row in records:
            status = str(row.get("status", "")).strip()

            if status != "pending":
                continue

            if rooms and str(row.get("classroom")) not in rooms:
                continue

            questions.append({
                "question_id": row.get("question_id"),
                "created_at": row.get("created_at"),
                "student_line_user_id": row.get("student_line_user_id"),
                "student_name": row.get("student_name"),
                "classroom": row.get("classroom"),
                "question_text": row.get("question_text")
            })

        return jsonify(success=True, questions=questions)

    except Exception as e:
        print("api_teacher_questions error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/questions/answer", methods=["POST"])
def api_teacher_answer_question():
    try:
        data = request.get_json()

        question_id = data.get("question_id", "").strip()
        answer_text = data.get("answer_text", "").strip()
        teacher_line_user_id = data.get("teacher_line_user_id", "").strip()

        if not question_id:
            return jsonify(success=False, message="ไม่พบ question_id"), 400

        if not answer_text:
            return jsonify(success=False, message="กรุณากรอกคำตอบ"), 400

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        ws = spreadsheet.worksheet("questions")
        records = ws.get_all_records()

        target_row = None
        student_line_user_id = ""

        for i, row in enumerate(records, start=2):
            if str(row.get("question_id")) == question_id:
                target_row = i
                student_line_user_id = row.get("student_line_user_id", "")
                break

        if not target_row:
            return jsonify(success=False, message="ไม่พบคำถามนี้"), 404

        ws.update(
            f"G{target_row}:J{target_row}",
            [[
                "answered",
                answer_text,
                now_text(),
                teacher_line_user_id
            ]]
        )

        # ถ้าต้องการ push กลับนักเรียน เปิดใช้ภายหลังได้
        # if student_line_user_id:
        #     line_bot_api.push_message(
        #         student_line_user_id,
        #         TextSendMessage(text=f"ครูตอบคำถามแล้ว:\n{answer_text}")
        #     )

        return jsonify(success=True, message="ตอบคำถามเรียบร้อยแล้ว")

    except Exception as e:
        print("api_teacher_answer_question error:", e)
        return jsonify(success=False, message=str(e)), 500


@app.route("/api/teacher/announce", methods=["POST"])
def api_teacher_announce():
    try:
        data = request.get_json()

        teacher_line_user_id = data.get("teacher_line_user_id", "").strip()
        teacher_name = data.get("teacher_name", "").strip()
        classroom = data.get("classroom", "").strip()
        message = data.get("message", "").strip()

        if not teacher_line_user_id:
            return jsonify(success=False, message="ไม่พบ LINE userId"), 400

        if not classroom:
            return jsonify(success=False, message="กรุณาเลือกห้อง"), 400

        if not message:
            return jsonify(success=False, message="กรุณากรอกข้อความประกาศ"), 400

        spreadsheet = get_spreadsheet()
        ensure_main_sheets(spreadsheet)

        if not teacher_name:
            teacher_name = get_teacher_name(spreadsheet, teacher_line_user_id)

        ws = spreadsheet.worksheet("announcements")
        announcement_id = "N" + uuid.uuid4().hex[:10]

        ws.append_row([
            announcement_id,
            now_text(),
            teacher_line_user_id,
            teacher_name,
            classroom,
            message
        ])

        return jsonify(
            success=True,
            message="บันทึกประกาศเรียบร้อยแล้ว",
            announcement_id=announcement_id
        )

    except Exception as e:
        print("api_teacher_announce error:", e)
        return jsonify(success=False, message=str(e)), 500


# =========================
# LINE Webhook
# =========================

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    reply_token = event.reply_token

    if user_text == "ทดสอบ":
        reply_text(reply_token, "บอททำงานปกติครับ")
        return

    reply_text(
        reply_token,
        "ได้รับข้อความแล้วครับ"
    )


# =========================
# Run local
# =========================

if __name__ == "__main__":
    app.run(debug=True)