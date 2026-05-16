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

from google_sheets import (
    append_user,
    append_submission,
    get_pending_assignments,
    get_latest_announcements
)


# =========================
# CONFIG
# =========================

load_dotenv()

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
REGISTER_RICH_MENU_ID = os.getenv("REGISTER_RICH_MENU_ID")
MAIN_RICH_MENU_ID = os.getenv("MAIN_RICH_MENU_ID")

if not CHANNEL_ACCESS_TOKEN:
    print("WARNING: CHANNEL_ACCESS_TOKEN not found")

if not CHANNEL_SECRET:
    print("WARNING: CHANNEL_SECRET not found")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

DB_NAME = "database.db"


# =========================
# RICH MENU
# =========================

def link_register_menu(user_id):
    try:
        if REGISTER_RICH_MENU_ID:
            line_bot_api.link_rich_menu_to_user(user_id, REGISTER_RICH_MENU_ID)
    except Exception as e:
        print("link_register_menu error:", e)


def link_main_menu(user_id):
    try:
        if MAIN_RICH_MENU_ID:
            line_bot_api.link_rich_menu_to_user(user_id, MAIN_RICH_MENU_ID)
    except Exception as e:
        print("link_main_menu error:", e)


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


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    cur.execute("""
    SELECT state, data
    FROM states
    WHERE line_user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def clear_state(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    DELETE FROM states
    WHERE line_user_id = ?
    """, (user_id,))
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


def register_user_sqlite(user_id, full_name, student_code, classroom):
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
        now_text()
    ))
    conn.commit()
    conn.close()


def save_submission_sqlite(user_id, homework_title, message_type, message_id, file_name=""):
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
        now_text()
    ))
    conn.commit()
    conn.close()


def get_submission_count(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT COUNT(*)
    FROM submissions
    WHERE line_user_id = ?
    """, (user_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count


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
# MESSAGE HELPERS
# =========================

def main_menu_text():
    return TextSendMessage(
        text="เมนูหลัก\n\nเลือกคำสั่งที่ต้องการ",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="ส่งงาน", text="ส่งงาน")),
            QuickReplyButton(action=MessageAction(label="งานค้าง", text="งานค้าง")),
            QuickReplyButton(action=MessageAction(label="ประกาศ", text="ประกาศจากครู")),
            QuickReplyButton(action=MessageAction(label="ถามครูนัท", text="ถามครูนัท")),
        ])
    )


def register_guide_message():
    return TextSendMessage(
        text=(
            "ระบบลงทะเบียน\n\n"
            "กรุณาพิมพ์ตามรูปแบบนี้:\n\n"
            "สมัคร ชื่อ-นามสกุล เลขที่ ห้อง\n\n"
            "ตัวอย่าง:\n"
            "สมัคร สมชาย ใจดี 1 401\n\n"
            "หมายเหตุ:\n"
            "เลขที่ = เลขที่ในห้อง\n"
            "ห้อง = ชื่อแท็บ เช่น 401"
        )
    )


def need_register_message():
    return TextSendMessage(
        text=(
            "คุณยังไม่ได้ลงทะเบียน\n\n"
            "กรุณากดเมนูลงทะเบียนด้านล่าง หรือพิมพ์:\n"
            "ลงทะเบียน"
        )
    )


def require_registered(event):
    user_id = event.source.user_id
    user = get_user(user_id)

    if not user:
        link_register_menu(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            need_register_message()
        )
        return False

    return True


# =========================
# LINE EVENTS
# =========================

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id

    user = get_user(user_id)

    if user:
        link_main_menu(user_id)
        text = "ยินดีต้อนรับกลับเข้าสู่ระบบส่งงาน\n\nกดเมนูด้านล่างเพื่อใช้งานได้เลย"
    else:
        link_register_menu(user_id)
        text = "ยินดีต้อนรับเข้าสู่ระบบส่งงาน\n\nกรุณากดเมนูด้านล่างเพื่อเริ่มลงทะเบียน"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=text)
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id

    state_row = get_state(user_id)

    # =========================
    # เมนู
    # =========================

    if user_text in ["เมนู", "menu", "Menu"]:
        clear_state(user_id)

        if get_user(user_id):
            link_main_menu(user_id)
            line_bot_api.reply_message(event.reply_token, main_menu_text())
        else:
            link_register_menu(user_id)
            line_bot_api.reply_message(event.reply_token, register_guide_message())
        return

    # =========================
    # ลงทะเบียน
    # =========================

    if user_text == "ลงทะเบียน":
        clear_state(user_id)
        link_register_menu(user_id)
        line_bot_api.reply_message(event.reply_token, register_guide_message())
        return

    # =========================
    # สมัคร
    # รูปแบบ: สมัคร ชื่อ นามสกุล เลขที่ ห้อง
    # =========================

    if user_text.startswith("สมัคร "):
        parts = user_text.split()

        if len(parts) < 4:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "รูปแบบไม่ถูกต้อง\n\n"
                        "ตัวอย่าง:\n"
                        "สมัคร สมชาย ใจดี 1 401"
                    )
                )
            )
            return

        classroom = parts[-1]
        student_code = parts[-2]
        full_name = " ".join(parts[1:-2])

        register_user_sqlite(user_id, full_name, student_code, classroom)

        append_user(
            now_text(),
            user_id,
            full_name,
            student_code,
            classroom,
            "student"
        )

        clear_state(user_id)
        link_main_menu(user_id)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "ลงทะเบียนสำเร็จ\n\n"
                    f"ชื่อ: {full_name}\n"
                    f"เลขที่: {student_code}\n"
                    f"ห้อง: {classroom}\n\n"
                    "ตอนนี้สามารถใช้เมนูหลักด้านล่างได้แล้ว"
                )
            )
        )
        return

    # =========================
    # ถ้ายังไม่ลงทะเบียน ห้ามใช้เมนูอื่น
    # =========================

    if user_text in ["ส่งงาน", "งานค้าง", "ประกาศจากครู", "ถามครูนัท", "สถานะของฉัน"]:
        if not require_registered(event):
            return

    # =========================
    # ส่งงาน
    # =========================

    if user_text == "ส่งงาน":
        clear_state(user_id)
        save_state(user_id, "waiting_homework_title")

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "ระบบส่งงาน\n\n"
                    "กรุณาพิมพ์ชื่องานก่อน\n\n"
                    "ตัวอย่าง:\n"
                    "งานที่ 1\n\n"
                    "หมายเหตุ: ชื่องานควรตรงกับในชีต assignments"
                )
            )
        )
        return

    # =========================
    # งานค้าง
    # =========================

    if user_text == "งานค้าง":
        user = get_user(user_id)

        if not user:
            link_register_menu(user_id)
            line_bot_api.reply_message(event.reply_token, need_register_message())
            return

        _, role, full_name, student_code, classroom = user

        pending = get_pending_assignments(user_id, classroom)

        if len(pending) == 0:
            text = "งานค้าง\n\nตอนนี้ยังไม่มีงานค้าง"
        else:
            lines = ["งานค้างของคุณ\n"]

            for i, item in enumerate(pending, start=1):
                title = item.get("title", "")
                due_date = item.get("due_date", "")
                max_score = item.get("max_score", "")

                lines.append(
                    f"{i}. {title}\n"
                    f"กำหนดส่ง: {due_date or '-'}\n"
                    f"คะแนนเต็ม: {max_score or '-'}"
                )

            text = "\n\n".join(lines)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=text)
        )
        return

    # =========================
    # ประกาศจากครู
    # =========================

    if user_text == "ประกาศจากครู":
        announcements = get_latest_announcements(limit=5)

        if not announcements:
            text = "ประกาศจากครู\n\nตอนนี้ยังไม่มีประกาศ"
        else:
            lines = ["ประกาศจากครู\n"]

            for i, row in enumerate(announcements, start=1):
                created_at = row.get("created_at", "")
                title = row.get("title", "")
                body = row.get("body", "")

                lines.append(
                    f"{i}. {title}\n"
                    f"{body}\n"
                    f"วันที่: {created_at}"
                )

            text = "\n\n".join(lines)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=text)
        )
        return

    # =========================
    # ถามครูนัท
    # =========================

    if user_text == "ถามครูนัท":
        save_state(user_id, "waiting_question")

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "ถามครูนัท\n\n"
                    "พิมพ์คำถามที่ต้องการถามได้เลย\n"
                    "เช่น ไม่เข้าใจการบ้านข้อ 3"
                )
            )
        )
        return

    # =========================
    # สถานะของฉัน
    # =========================

    if user_text == "สถานะของฉัน":
        user = get_user(user_id)

        if not user:
            link_register_menu(user_id)
            line_bot_api.reply_message(event.reply_token, need_register_message())
            return

        _, role, full_name, student_code, classroom = user
        count = get_submission_count(user_id)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "ข้อมูลของฉัน\n\n"
                    f"ชื่อ: {full_name}\n"
                    f"เลขที่: {student_code}\n"
                    f"ห้อง: {classroom}\n"
                    f"จำนวนงานที่ส่ง: {count}"
                )
            )
        )
        return

    # =========================
    # จัดการ state
    # =========================

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

        if state == "waiting_question":
            question = user_text
            clear_state(user_id)

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "รับคำถามแล้ว\n\n"
                        f"คำถามของคุณ:\n{question}\n\n"
                        "ตอนนี้ระบบยังไม่ได้เชื่อม AI หรือส่งต่อให้ครูจริง\n"
                        "ขั้นต่อไปสามารถเพิ่มระบบแจ้งเตือนครูได้"
                    )
                )
            )
            return

    # =========================
    # fallback
    # =========================

    if get_user(user_id):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="ไม่พบคำสั่ง\nกรุณากดเมนูด้านล่าง หรือพิมพ์: เมนู")
        )
    else:
        link_register_menu(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            need_register_message()
        )


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id

    if not require_registered(event):
        return

    state_row = get_state(user_id)

    if not state_row or state_row[0] != "waiting_homework_file":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="หากต้องการส่งงาน กรุณากดเมนู ส่งงาน ก่อน")
        )
        return

    homework_title = state_row[1]
    message_id = event.message.id

    user = get_user(user_id)
    _, role, full_name, student_code, classroom = user

    save_submission_sqlite(
        user_id=user_id,
        homework_title=homework_title,
        message_type="image",
        message_id=message_id,
        file_name=""
    )

    append_submission(
        now_text(),
        user_id,
        full_name,
        student_code,
        classroom,
        homework_title,
        "image",
        message_id,
        ""
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

    if not require_registered(event):
        return

    state_row = get_state(user_id)

    if not state_row or state_row[0] != "waiting_homework_file":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="หากต้องการส่งงาน กรุณากดเมนู ส่งงาน ก่อน")
        )
        return

    homework_title = state_row[1]
    message_id = event.message.id
    file_name = event.message.file_name

    user = get_user(user_id)
    _, role, full_name, student_code, classroom = user

    save_submission_sqlite(
        user_id=user_id,
        homework_title=homework_title,
        message_type="file",
        message_id=message_id,
        file_name=file_name
    )

    append_submission(
        now_text(),
        user_id,
        full_name,
        student_code,
        classroom,
        homework_title,
        "file",
        message_id,
        file_name
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