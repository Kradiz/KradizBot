from flask import Flask, request, abort
from dotenv import load_dotenv
import os
import sqlite3
from datetime import datetime

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    ImageMessage,
    FileMessage,
    FollowEvent,
    QuickReply,
    QuickReplyButton,
    MessageAction
)

load_dotenv()

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

DB_NAME = "database.db"


# =========================
# DATABASE
# =========================

def get_conn():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        line_user_id TEXT UNIQUE,
        role TEXT DEFAULT 'student',
        full_name TEXT,
        student_code TEXT,
        classroom TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        line_user_id TEXT,
        homework_title TEXT,
        message_type TEXT,
        line_message_id TEXT,
        file_name TEXT,
        submitted_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS states (
        line_user_id TEXT PRIMARY KEY,
        state TEXT,
        data TEXT
    )
    """)

    conn.commit()
    conn.close()


init_db()


def save_state(user_id, state, data=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO states (line_user_id, state, data)
    VALUES (?, ?, ?)
    """, (user_id, state, data))
    conn.commit()
    conn.close()


def get_state(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT state, data FROM states WHERE line_user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def clear_state(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM states WHERE line_user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT line_user_id, role, full_name, student_code, classroom
    FROM users
    WHERE line_user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def register_user(user_id, full_name, student_code, classroom):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO users
    (line_user_id, role, full_name, student_code, classroom, created_at)
    VALUES (?, 'student', ?, ?, ?, ?)
    """, (
        user_id,
        full_name,
        student_code,
        classroom,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()


def save_submission(user_id, homework_title, message_type, message_id, file_name=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO submissions
    (line_user_id, homework_title, message_type, line_message_id, file_name, submitted_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        homework_title,
        message_type,
        message_id,
        file_name,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()


# =========================
# FLASK ROUTES
# =========================

@app.route("/")
def home():
    return "LINE SCHOOL BOT WORKING"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# =========================
# REPLY HELPERS
# =========================

def main_menu_text():
    return TextSendMessage(
        text="เมนูหลัก\n\nเลือกคำสั่งที่ต้องการ",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="ลงทะเบียน", text="ลงทะเบียน")),
            QuickReplyButton(action=MessageAction(label="ส่งงาน", text="ส่งงาน")),
            QuickReplyButton(action=MessageAction(label="สถานะของฉัน", text="สถานะของฉัน")),
        ])
    )


def reply_register_guide(event):
    text = (
        "ระบบลงทะเบียน\n\n"
        "กรุณาพิมพ์ตามรูปแบบนี้:\n\n"
        "สมัคร ชื่อ-นามสกุล รหัสนักเรียน ห้อง\n\n"
        "ตัวอย่าง:\n"
        "สมัคร สมชาย ใจดี 65001 ม.5/1"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))


def reply_submit_guide(event):
    user_id = event.source.user_id

    user = get_user(user_id)
    if not user:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="กรุณาลงทะเบียนก่อนส่งงาน\nพิมพ์: ลงทะเบียน")
        )
        return

    save_state(user_id, "waiting_homework_title")

    text = (
        "ระบบส่งงาน\n\n"
        "กรุณาพิมพ์ชื่องานก่อน\n\n"
        "ตัวอย่าง:\n"
        "ใบงานที่ 1"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))


# =========================
# LINE EVENTS
# =========================

@handler.add(FollowEvent)
def handle_follow(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text="ยินดีต้อนรับเข้าสู่ระบบส่งงาน\n\nพิมพ์: เมนู"
        )
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id

    state_row = get_state(user_id)

    if user_text in ["เมนู", "menu", "Menu"]:
        clear_state(user_id)
        line_bot_api.reply_message(event.reply_token, main_menu_text())
        return

    if user_text == "ลงทะเบียน":
        clear_state(user_id)
        reply_register_guide(event)
        return

    if user_text.startswith("สมัคร "):
        parts = user_text.split()

        if len(parts) < 4:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="รูปแบบไม่ถูกต้อง\n\nตัวอย่าง:\nสมัคร สมชาย ใจดี 65001 ม.5/1"
                )
            )
            return

        # รองรับชื่อหลายคำ: สมัคร ชื่อ นามสกุล รหัส ห้อง
        classroom = parts[-1]
        student_code = parts[-2]
        full_name = " ".join(parts[1:-2])

        register_user(user_id, full_name, student_code, classroom)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "ลงทะเบียนสำเร็จ\n\n"
                    f"ชื่อ: {full_name}\n"
                    f"รหัส: {student_code}\n"
                    f"ห้อง: {classroom}\n\n"
                    "ต่อไปสามารถพิมพ์: ส่งงาน"
                )
            )
        )
        return

    if user_text == "ส่งงาน":
        clear_state(user_id)
        reply_submit_guide(event)
        return

    if user_text == "สถานะของฉัน":
        user = get_user(user_id)

        if not user:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="ยังไม่ได้ลงทะเบียน\nพิมพ์: ลงทะเบียน")
            )
            return

        _, role, full_name, student_code, classroom = user

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
        SELECT COUNT(*) FROM submissions
        WHERE line_user_id = ?
        """, (user_id,))
        count = cur.fetchone()[0]
        conn.close()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "ข้อมูลของฉัน\n\n"
                    f"ชื่อ: {full_name}\n"
                    f"รหัส: {student_code}\n"
                    f"ห้อง: {classroom}\n"
                    f"จำนวนงานที่ส่ง: {count}"
                )
            )
        )
        return

    # ถ้าอยู่ในขั้นตอนส่งงาน
    if state_row:
        state, data = state_row

        if state == "waiting_homework_title":
            homework_title = user_text
            save_state(user_id, "waiting_homework_file", homework_title)

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        f"ชื่องาน: {homework_title}\n\n"
                        "กรุณาส่งรูปภาพหรือไฟล์งานมาได้เลย"
                    )
                )
            )
            return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="ไม่พบคำสั่ง\nพิมพ์: เมนู")
    )


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    state_row = get_state(user_id)

    if not state_row or state_row[0] != "waiting_homework_file":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="หากต้องการส่งงาน กรุณาพิมพ์: ส่งงาน")
        )
        return

    homework_title = state_row[1]
    message_id = event.message.id

    save_submission(
        user_id=user_id,
        homework_title=homework_title,
        message_type="image",
        message_id=message_id,
        file_name=""
    )

    clear_state(user_id)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=(
                "รับงานเรียบร้อย\n\n"
                f"ชื่องาน: {homework_title}\n"
                "ประเภท: รูปภาพ"
            )
        )
    )


@handler.add(MessageEvent, message=FileMessage)
def handle_file(event):
    user_id = event.source.user_id
    state_row = get_state(user_id)

    if not state_row or state_row[0] != "waiting_homework_file":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="หากต้องการส่งงาน กรุณาพิมพ์: ส่งงาน")
        )
        return

    homework_title = state_row[1]
    message_id = event.message.id
    file_name = event.message.file_name

    save_submission(
        user_id=user_id,
        homework_title=homework_title,
        message_type="file",
        message_id=message_id,
        file_name=file_name
    )

    clear_state(user_id)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=(
                "รับงานเรียบร้อย\n\n"
                f"ชื่องาน: {homework_title}\n"
                f"ไฟล์: {file_name}"
            )
        )
    )


if __name__ == "__main__":
    app.run(debug=True)