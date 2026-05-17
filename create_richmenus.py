import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv


# =========================================================
# Path / Load .env
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
RICHMENU_DIR = BASE_DIR / "richmenus"

load_dotenv(dotenv_path=ENV_PATH)


# =========================================================
# Debug ENV
# =========================================================

print("========================================")
print(" ENV CHECK")
print("========================================")
print(f"BASE_DIR   = {BASE_DIR}")
print(f"ENV_PATH   = {ENV_PATH}")
print(f"ENV EXISTS = {ENV_PATH.exists()}")
print("========================================")


# =========================================================
# Required ENV
# =========================================================

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()


# =========================================================
# LIFF IDs
# =========================================================

LIFF_TEACHER_SETUP_ID = os.getenv("LIFF_TEACHER_SETUP_ID", "").strip()
LIFF_TEACHER_ASSIGNMENT_ID = os.getenv("LIFF_TEACHER_ASSIGNMENT_ID", "").strip()
LIFF_TEACHER_QUESTIONS_ID = os.getenv("LIFF_TEACHER_QUESTIONS_ID", "").strip()
LIFF_TEACHER_PENDING_ID = os.getenv("LIFF_TEACHER_PENDING_ID", "").strip()
LIFF_TEACHER_ANNOUNCE_ID = os.getenv("LIFF_TEACHER_ANNOUNCE_ID", "").strip()

LIFF_STUDENT_REGISTER_ID = os.getenv("LIFF_STUDENT_REGISTER_ID", "").strip()
LIFF_STUDENT_SUBMIT_ID = os.getenv("LIFF_STUDENT_SUBMIT_ID", "").strip()
LIFF_STUDENT_QUESTION_ID = os.getenv("LIFF_STUDENT_QUESTION_ID", "").strip()
LIFF_STUDENT_PENDING_ID = os.getenv("LIFF_STUDENT_PENDING_ID", "").strip()
LIFF_STUDENT_ANNOUNCE_ID = os.getenv("LIFF_STUDENT_ANNOUNCE_ID", "").strip()


# =========================================================
# Rich Menu ENV Keys
# =========================================================

RICHMENU_ENV_KEYS = [
    "TEACHER_RICH_MENU_SETUP_ID",
    "TEACHER_RICH_MENU_NORMAL_ID",
    "TEACHER_RICH_MENU_QUESTION_ALERT_ID",

    "STUDENT_RICH_MENU_REGISTER_ID",
    "STUDENT_RICH_MENU_NORMAL_ID",
    "STUDENT_RICH_MENU_PENDING_ALERT_ID",
    "STUDENT_RICH_MENU_ANSWER_ALERT_ID",
    "STUDENT_RICH_MENU_BOTH_ALERT_ID",
]


# =========================================================
# LINE API
# =========================================================

LINE_API_BASE = "https://api.line.me/v2/bot"
LINE_API_DATA_BASE = "https://api-data.line.me/v2/bot"


def line_headers_json():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }


def line_headers_image(image_path):
    """
    เลือก Content-Type ตามนามสกุลไฟล์
    .jpg/.jpeg = image/jpeg
    .png = image/png
    """
    suffix = image_path.suffix.lower()

    if suffix in [".jpg", ".jpeg"]:
        content_type = "image/jpeg"
    else:
        content_type = "image/png"

    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": content_type
    }


# =========================================================
# Utility
# =========================================================

def require_env(name, value):
    if not value:
        raise Exception(f"Missing ENV: {name}")


def liff_url(liff_id):
    if not liff_id:
        return ""
    return f"https://liff.line.me/{liff_id}"


def read_env_file():
    """
    อ่าน .env เป็น dict แบบง่าย ๆ
    """
    env_data = {}

    if not ENV_PATH.exists():
        return env_data

    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")

            if not raw.strip():
                continue

            if raw.strip().startswith("#"):
                continue

            if "=" not in raw:
                continue

            key, value = raw.split("=", 1)
            env_data[key.strip()] = value.strip()

    return env_data


def update_env_file(new_values):
    """
    อัปเดตค่าใน .env
    ถ้ามี key เดิมให้แทนที่
    ถ้าไม่มีให้เพิ่มท้ายไฟล์
    """
    lines = []

    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    existing_keys = set()
    updated_lines = []

    for line in lines:
        raw = line.rstrip("\n")

        if not raw.strip() or raw.strip().startswith("#") or "=" not in raw:
            updated_lines.append(line)
            continue

        key, old_value = raw.split("=", 1)
        key = key.strip()

        if key in new_values:
            updated_lines.append(f"{key}={new_values[key]}\n")
            existing_keys.add(key)
        else:
            updated_lines.append(line)

    missing = [k for k in new_values.keys() if k not in existing_keys]

    if missing:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] += "\n"

        updated_lines.append("\n# Rich Menu IDs generated by create_richmenus.py\n")

        for key in missing:
            updated_lines.append(f"{key}={new_values[key]}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)

    print(f"[ENV] Updated .env: {ENV_PATH}")


def check_image_exists(filename):
    path = RICHMENU_DIR / filename

    if not path.exists():
        raise Exception(f"Missing image file: {path}")

    return path


# =========================================================
# LINE Rich Menu Functions
# =========================================================

def delete_rich_menu(rich_menu_id):
    """
    ลบ Rich Menu ตาม ID
    ถ้า ID ไม่มีหรือถูกลบไปแล้ว จะข้าม
    """
    if not rich_menu_id:
        return False

    url = f"{LINE_API_BASE}/richmenu/{rich_menu_id}"

    try:
        res = requests.delete(url, headers=line_headers_json(), timeout=20)

        if res.status_code in [200, 204]:
            print(f"[DELETE] Deleted rich menu: {rich_menu_id}")
            return True

        if res.status_code == 404:
            print(f"[DELETE] Rich menu not found, skip: {rich_menu_id}")
            return False

        print(f"[DELETE] Failed {rich_menu_id}: {res.status_code} {res.text}")
        return False

    except Exception as e:
        print(f"[DELETE] Error {rich_menu_id}: {e}")
        return False


def delete_old_richmenus_from_env():
    """
    ลบ Rich Menu เก่าจากค่าใน .env
    """
    env_data = read_env_file()

    print("\n========== Delete old rich menus from .env ==========")

    for key in RICHMENU_ENV_KEYS:
        old_id = env_data.get(key, "").strip()

        if old_id:
            print(f"[OLD] {key}={old_id}")
            delete_rich_menu(old_id)
            time.sleep(0.3)
        else:
            print(f"[OLD] {key}=<empty> skip")


def create_rich_menu(payload):
    """
    สร้าง Rich Menu และคืน richMenuId
    """
    url = f"{LINE_API_BASE}/richmenu"

    res = requests.post(
        url,
        headers=line_headers_json(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=30
    )

    if res.status_code not in [200, 201]:
        raise Exception(f"Create rich menu failed: {res.status_code} {res.text}")

    data = res.json()
    rich_menu_id = data["richMenuId"]

    print(f"[CREATE] {payload.get('name')} -> {rich_menu_id}")

    return rich_menu_id


def upload_rich_menu_image(rich_menu_id, image_path):
    """
    อัปโหลดรูปให้ Rich Menu
    ใช้ api-data.line.me
    รองรับทั้ง JPG และ PNG
    """
    url = f"{LINE_API_DATA_BASE}/richmenu/{rich_menu_id}/content"

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    res = requests.post(
        url,
        headers=line_headers_image(image_path),
        data=image_bytes,
        timeout=60
    )

    if res.status_code not in [200, 201]:
        raise Exception(
            f"Upload image failed: {rich_menu_id}: {res.status_code} {res.text}"
        )

    print(f"[UPLOAD] {image_path.name} -> {rich_menu_id}")


# =========================================================
# Rich Menu Payload Builders
# =========================================================

def action_uri(label, uri):
    return {
        "type": "uri",
        "label": label,
        "uri": uri
    }


def build_one_button_richmenu(name, chat_bar_text, uri):
    """
    เมนูแบบปุ่มเดียวเต็มรูป
    ใช้กับ:
    - teacher setup
    - student register
    """
    return {
        "size": {
            "width": 2500,
            "height": 1686
        },
        "selected": True,
        "name": name,
        "chatBarText": chat_bar_text,
        "areas": [
            {
                "bounds": {
                    "x": 0,
                    "y": 0,
                    "width": 2500,
                    "height": 1686
                },
                "action": action_uri(chat_bar_text, uri)
            }
        ]
    }


def build_teacher_main_richmenu(name):
    """
    เมนูครู 4 ช่อง:
    สั่งงาน        คำถาม
    งานค้าง        ประกาศ
    """
    return {
        "size": {
            "width": 2500,
            "height": 1686
        },
        "selected": True,
        "name": name,
        "chatBarText": "เมนูครู",
        "areas": [
            {
                "bounds": {
                    "x": 0,
                    "y": 0,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "สั่งงาน",
                    liff_url(LIFF_TEACHER_ASSIGNMENT_ID)
                )
            },
            {
                "bounds": {
                    "x": 1250,
                    "y": 0,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "คำถาม",
                    liff_url(LIFF_TEACHER_QUESTIONS_ID)
                )
            },
            {
                "bounds": {
                    "x": 0,
                    "y": 843,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "งานค้าง",
                    liff_url(LIFF_TEACHER_PENDING_ID)
                )
            },
            {
                "bounds": {
                    "x": 1250,
                    "y": 843,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "ประกาศ",
                    liff_url(LIFF_TEACHER_ANNOUNCE_ID)
                )
            }
        ]
    }


def build_student_main_richmenu(name):
    """
    เมนูนักเรียน 4 ช่อง:
    ส่งงาน        ถามคำถาม
    งานค้าง       ประกาศ
    """
    return {
        "size": {
            "width": 2500,
            "height": 1686
        },
        "selected": True,
        "name": name,
        "chatBarText": "เมนูนักเรียน",
        "areas": [
            {
                "bounds": {
                    "x": 0,
                    "y": 0,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "ส่งงาน",
                    liff_url(LIFF_STUDENT_SUBMIT_ID)
                )
            },
            {
                "bounds": {
                    "x": 1250,
                    "y": 0,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "ถามคำถาม",
                    liff_url(LIFF_STUDENT_QUESTION_ID)
                )
            },
            {
                "bounds": {
                    "x": 0,
                    "y": 843,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "งานค้าง",
                    liff_url(LIFF_STUDENT_PENDING_ID)
                )
            },
            {
                "bounds": {
                    "x": 1250,
                    "y": 843,
                    "width": 1250,
                    "height": 843
                },
                "action": action_uri(
                    "ประกาศ",
                    liff_url(LIFF_STUDENT_ANNOUNCE_ID)
                )
            }
        ]
    }


# =========================================================
# Render API
# =========================================================

def render_headers():
    return {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json"
    }


def update_render_env_vars(new_values):
    """
    อัปเดต Environment Variables บน Render

    ต้องมี:
    RENDER_API_KEY
    RENDER_SERVICE_ID

    ถ้าไม่ได้ใส่ไว้ จะข้ามให้ ไม่ error
    """
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        print("[RENDER] Missing RENDER_API_KEY or RENDER_SERVICE_ID, skip Render env update.")
        return False

    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars"

    payload = []

    for key, value in new_values.items():
        payload.append({
            "key": key,
            "value": value
        })

    try:
        res = requests.patch(
            url,
            headers=render_headers(),
            json=payload,
            timeout=30
        )

        if res.status_code in [200, 201]:
            print("[RENDER] Updated Render environment variables successfully.")
            return True

        print(f"[RENDER] Failed to update env vars: {res.status_code} {res.text}")
        print("[RENDER] ถ้า error ให้ copy ค่าใน .env ไปใส่ Render เองได้")
        return False

    except Exception as e:
        print(f"[RENDER] Error: {e}")
        return False


# =========================================================
# Main Create Flow
# =========================================================

def validate_before_run():
    print("\n========== Validate ENV ==========")

    require_env("LINE_CHANNEL_ACCESS_TOKEN", LINE_CHANNEL_ACCESS_TOKEN)

    require_env("LIFF_TEACHER_SETUP_ID", LIFF_TEACHER_SETUP_ID)
    require_env("LIFF_TEACHER_ASSIGNMENT_ID", LIFF_TEACHER_ASSIGNMENT_ID)
    require_env("LIFF_TEACHER_QUESTIONS_ID", LIFF_TEACHER_QUESTIONS_ID)
    require_env("LIFF_TEACHER_PENDING_ID", LIFF_TEACHER_PENDING_ID)
    require_env("LIFF_TEACHER_ANNOUNCE_ID", LIFF_TEACHER_ANNOUNCE_ID)

    require_env("LIFF_STUDENT_REGISTER_ID", LIFF_STUDENT_REGISTER_ID)
    require_env("LIFF_STUDENT_SUBMIT_ID", LIFF_STUDENT_SUBMIT_ID)
    require_env("LIFF_STUDENT_QUESTION_ID", LIFF_STUDENT_QUESTION_ID)
    require_env("LIFF_STUDENT_PENDING_ID", LIFF_STUDENT_PENDING_ID)
    require_env("LIFF_STUDENT_ANNOUNCE_ID", LIFF_STUDENT_ANNOUNCE_ID)

    print("[OK] ENV ครบ")


def validate_images():
    print("\n========== Validate images ==========")

    required_images = [
        "richmenu_teacher_setup.jpg",
        "richmenu_teacher_normal.jpg",
        "richmenu_teacher_question_alert.jpg",

        "richmenu_student_register.jpg",
        "richmenu_student_normal.jpg",
        "richmenu_student_pending_alert.jpg",
        "richmenu_student_answer_alert.jpg",
        "richmenu_student_both_alert.jpg",
    ]

    for img in required_images:
        path = check_image_exists(img)
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"[OK] {path.name} ({size_mb:.2f} MB)")

        if size_mb > 1.0:
            print(f"[WARN] {path.name} ใหญ่กว่า 1 MB อาจอัปโหลดไม่ผ่าน LINE")

    print("[OK] รูปครบ")


def create_all_richmenus():
    """
    สร้าง Rich Menu ทั้ง 8 แบบ
    ใช้ไฟล์ JPG เพื่อลดขนาดและเลี่ยง 413 Request Entity Too Large
    """
    result = {}

    richmenus = [
        {
            "env_key": "TEACHER_RICH_MENU_SETUP_ID",
            "name": "teacher_setup_menu",
            "image": "richmenu_teacher_setup.jpg",
            "payload": build_one_button_richmenu(
                "teacher_setup_menu",
                "ตั้งค่าห้อง",
                liff_url(LIFF_TEACHER_SETUP_ID)
            )
        },
        {
            "env_key": "TEACHER_RICH_MENU_NORMAL_ID",
            "name": "teacher_main_normal",
            "image": "richmenu_teacher_normal.jpg",
            "payload": build_teacher_main_richmenu(
                "teacher_main_normal"
            )
        },
        {
            "env_key": "TEACHER_RICH_MENU_QUESTION_ALERT_ID",
            "name": "teacher_main_question_alert",
            "image": "richmenu_teacher_question_alert.jpg",
            "payload": build_teacher_main_richmenu(
                "teacher_main_question_alert"
            )
        },
        {
            "env_key": "STUDENT_RICH_MENU_REGISTER_ID",
            "name": "student_register_menu",
            "image": "richmenu_student_register.jpg",
            "payload": build_one_button_richmenu(
                "student_register_menu",
                "ลงทะเบียน",
                liff_url(LIFF_STUDENT_REGISTER_ID)
            )
        },
        {
            "env_key": "STUDENT_RICH_MENU_NORMAL_ID",
            "name": "student_main_normal",
            "image": "richmenu_student_normal.jpg",
            "payload": build_student_main_richmenu(
                "student_main_normal"
            )
        },
        {
            "env_key": "STUDENT_RICH_MENU_PENDING_ALERT_ID",
            "name": "student_main_pending_alert",
            "image": "richmenu_student_pending_alert.jpg",
            "payload": build_student_main_richmenu(
                "student_main_pending_alert"
            )
        },
        {
            "env_key": "STUDENT_RICH_MENU_ANSWER_ALERT_ID",
            "name": "student_main_answer_alert",
            "image": "richmenu_student_answer_alert.jpg",
            "payload": build_student_main_richmenu(
                "student_main_answer_alert"
            )
        },
        {
            "env_key": "STUDENT_RICH_MENU_BOTH_ALERT_ID",
            "name": "student_main_both_alert",
            "image": "richmenu_student_both_alert.jpg",
            "payload": build_student_main_richmenu(
                "student_main_both_alert"
            )
        },
    ]

    print("\n========== Create new rich menus ==========")

    for item in richmenus:
        image_path = check_image_exists(item["image"])

        rich_menu_id = create_rich_menu(item["payload"])
        time.sleep(0.3)

        upload_rich_menu_image(rich_menu_id, image_path)
        time.sleep(0.3)

        result[item["env_key"]] = rich_menu_id

    return result


def print_result(new_values):
    print("\n========== New Rich Menu IDs ==========")

    for key, value in new_values.items():
        print(f"{key}={value}")

    print("\nคัดลอกชุดนี้ไปใส่ Render Environment ได้ ถ้าไม่ได้ใช้ Render API")


def main():
    print("========================================")
    print(" LINE Rich Menu Creator - 8 Menus")
    print("========================================")

    validate_before_run()
    validate_images()

    # 1. ลบของเก่าจาก .env
    delete_old_richmenus_from_env()

    # 2. สร้างของใหม่
    new_values = create_all_richmenus()

    # 3. เขียนลง .env
    update_env_file(new_values)

    # 4. อัปเดต Render env ถ้ามี API key
    update_render_env_vars(new_values)

    # 5. แสดงผล
    print_result(new_values)

    print("\nเสร็จแล้ว")
    print("ถ้าอัปเดต Render env สำเร็จ ให้ไปกด Manual Deploy หรือรอ redeploy")
    print("ถ้าไม่ได้ใช้ Render API ให้ copy ค่า Rich Menu IDs จาก .env ไปใส่ Render เอง")


if __name__ == "__main__":
    main()