import os
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

from linebot import LineBotApi
from linebot.models import (
    RichMenu,
    RichMenuSize,
    RichMenuArea,
    RichMenuBounds,
    URIAction,
    MessageAction,
)


# ==================================================
# โหลด .env
# ==================================================
load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
LIFF_REGISTER_URL = os.getenv("LIFF_REGISTER_URL")

if not CHANNEL_ACCESS_TOKEN:
    raise ValueError("ไม่พบ CHANNEL_ACCESS_TOKEN ในไฟล์ .env")

if not LIFF_REGISTER_URL:
    raise ValueError("ไม่พบ LIFF_REGISTER_URL ในไฟล์ .env")


line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)


# ==================================================
# Path / ขนาดรูป
# ==================================================
BASE_DIR = Path(__file__).resolve().parent

RICHMENU_WIDTH = 2500
RICHMENU_HEIGHT = 1686

REGISTER_SOURCE_IMAGE = BASE_DIR / "richmenu_register.png"
REGISTER_OUTPUT_IMAGE = BASE_DIR / "richmenu_register_2500x1686.jpg"

MAIN_SOURCE_IMAGE = BASE_DIR / "richmenu_main.png"
MAIN_OUTPUT_IMAGE = BASE_DIR / "richmenu_main_2500x1686.jpg"


# ==================================================
# ฟังก์ชันเตรียมรูป
# ==================================================
def prepare_image(source_path: Path, output_path: Path):
    """
    แปลงรูปเป็น 2500x1686 jpg และบีบอัดไม่เกิน 1 MB
    """

    if not source_path.exists():
        raise FileNotFoundError(
            f"ไม่พบไฟล์รูป: {source_path.name}\n"
            f"กรุณาวางไฟล์ไว้ที่:\n{source_path}"
        )

    print(f"\nกำลังเตรียมรูป: {source_path.name}")

    img = Image.open(source_path).convert("RGB")

    src_w, src_h = img.size
    print(f"ขนาดต้นฉบับ: {src_w} x {src_h}")

    target_ratio = RICHMENU_WIDTH / RICHMENU_HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # รูปกว้างเกิน ตัดซ้ายขวา
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
        print("ตัดซ้าย/ขวาให้ตรงสัดส่วน")
    else:
        # รูปสูงเกิน ตัดบนล่าง
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))
        print("ตัดบน/ล่างให้ตรงสัดส่วน")

    img = img.resize((RICHMENU_WIDTH, RICHMENU_HEIGHT), Image.LANCZOS)

    quality = 90

    while quality >= 50:
        img.save(output_path, "JPEG", quality=quality, optimize=True)
        size_mb = output_path.stat().st_size / (1024 * 1024)

        print(f"บันทึก quality={quality}, size={size_mb:.2f} MB")

        if size_mb <= 1.0:
            break

        quality -= 5

    if output_path.stat().st_size > 1024 * 1024:
        raise ValueError(
            f"ไฟล์ {output_path.name} ยังเกิน 1 MB\n"
            "ให้ใช้รูปพื้นหลังเรียบขึ้น หรือบีบอัดรูปก่อน"
        )

    print(f"เตรียมรูปสำเร็จ: {output_path.name}")


# ==================================================
# ลบ Rich Menu เก่าทั้งหมด
# ==================================================
def delete_all_rich_menus():
    print("\nกำลังดึงรายการ Rich Menu เก่าทั้งหมด...")

    rich_menus = line_bot_api.get_rich_menu_list()

    if not rich_menus:
        print("ไม่พบ Rich Menu เก่า")
        return

    print("รายการที่จะลบ:")

    for menu in rich_menus:
        print(f"- {menu.rich_menu_id} | {menu.name} | {menu.chat_bar_text}")

    print("\nกำลังลบ Rich Menu เก่าทั้งหมด...")

    for menu in rich_menus:
        try:
            print(f"ลบ: {menu.rich_menu_id} | {menu.name}")
            line_bot_api.delete_rich_menu(menu.rich_menu_id)
        except Exception as e:
            print(f"ลบไม่สำเร็จ: {menu.rich_menu_id} -> {e}")

    print("ลบ Rich Menu เก่าทั้งหมดเสร็จแล้ว")


# ==================================================
# สร้างเมนูลงทะเบียน
# ==================================================
def create_register_menu():
    """
    เมนูลงทะเบียน:
    ทั้งภาพเป็นปุ่มเดียว กดแล้วเปิด LIFF ลงทะเบียน
    """

    print("\nกำลังสร้าง Rich Menu ลงทะเบียน...")

    rich_menu = RichMenu(
        size=RichMenuSize(
            width=RICHMENU_WIDTH,
            height=RICHMENU_HEIGHT,
        ),
        selected=True,
        name="register-menu",
        chat_bar_text="ลงทะเบียน",
        areas=[
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=0,
                    y=0,
                    width=RICHMENU_WIDTH,
                    height=RICHMENU_HEIGHT,
                ),
                action=URIAction(
                    label="ลงทะเบียน",
                    uri=LIFF_REGISTER_URL,
                ),
            )
        ],
    )

    rich_menu_id = line_bot_api.create_rich_menu(rich_menu=rich_menu)

    print(f"สร้างเมนูลงทะเบียนสำเร็จ: {rich_menu_id}")

    print("กำลังอัปโหลดรูปเมนูลงทะเบียน...")
    with open(REGISTER_OUTPUT_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            rich_menu_id,
            "image/jpeg",
            f,
        )

    print("อัปโหลดรูปเมนูลงทะเบียนสำเร็จ")

    return rich_menu_id


# ==================================================
# สร้างเมนูหลัก
# ==================================================
def create_main_menu():
    """
    เมนูหลัก 4 ปุ่ม:
    ซ้ายบน    = ส่งงาน
    ขวาบน     = ถามคำถาม
    ซ้ายล่าง  = เช็คงานค้าง
    ขวาล่าง   = ประกาศ

    ใช้ MessageAction ก่อน
    พอกดแล้วจะส่งข้อความเข้า webhook
    """

    print("\nกำลังสร้าง Rich Menu หลัก...")

    half_w = RICHMENU_WIDTH // 2
    half_h = RICHMENU_HEIGHT // 2

    rich_menu = RichMenu(
        size=RichMenuSize(
            width=RICHMENU_WIDTH,
            height=RICHMENU_HEIGHT,
        ),
        selected=True,
        name="main-menu",
        chat_bar_text="เมนูระบบส่งงาน",
        areas=[
            # ซ้ายบน: ส่งงาน
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=0,
                    y=0,
                    width=half_w,
                    height=half_h,
                ),
                action=MessageAction(
                    label="ส่งงาน",
                    text="ส่งงาน",
                ),
            ),

            # ขวาบน: ถามคำถาม
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=half_w,
                    y=0,
                    width=half_w,
                    height=half_h,
                ),
                action=MessageAction(
                    label="ถามคำถาม",
                    text="ถามคำถาม",
                ),
            ),

            # ซ้ายล่าง: เช็คงานค้าง
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=0,
                    y=half_h,
                    width=half_w,
                    height=half_h,
                ),
                action=MessageAction(
                    label="เช็คงานค้าง",
                    text="เช็คงานค้าง",
                ),
            ),

            # ขวาล่าง: ประกาศ
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=half_w,
                    y=half_h,
                    width=half_w,
                    height=half_h,
                ),
                action=MessageAction(
                    label="ประกาศ",
                    text="ประกาศ",
                ),
            ),
        ],
    )

    rich_menu_id = line_bot_api.create_rich_menu(rich_menu=rich_menu)

    print(f"สร้างเมนูหลักสำเร็จ: {rich_menu_id}")

    print("กำลังอัปโหลดรูปเมนูหลัก...")
    with open(MAIN_OUTPUT_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            rich_menu_id,
            "image/jpeg",
            f,
        )

    print("อัปโหลดรูปเมนูหลักสำเร็จ")

    return rich_menu_id


# ==================================================
# ตั้ง Default เป็นเมนูลงทะเบียน
# ==================================================
def set_default_register_menu(register_menu_id):
    print("\nกำลังตั้งเมนูลงทะเบียนเป็น Default Rich Menu...")

    line_bot_api.set_default_rich_menu(register_menu_id)

    print("ตั้ง Default Rich Menu สำเร็จ")


# ==================================================
# อัปเดต .env
# ==================================================
def update_env_ids(register_menu_id, main_menu_id):
    env_path = BASE_DIR / ".env"

    if not env_path.exists():
        raise FileNotFoundError("ไม่พบไฟล์ .env")

    lines = env_path.read_text(encoding="utf-8").splitlines()

    updates = {
        "REGISTER_RICH_MENU_ID": register_menu_id,
        "MAIN_RICH_MENU_ID": main_menu_id,
    }

    found_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()

        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        key = line.split("=", 1)[0].strip()

        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            found_keys.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in found_keys:
            new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    print("\nอัปเดต .env สำเร็จ")
    print(f"REGISTER_RICH_MENU_ID={register_menu_id}")
    print(f"MAIN_RICH_MENU_ID={main_menu_id}")


# ==================================================
# แสดงรายการ Rich Menu
# ==================================================
def list_rich_menus():
    print("\nรายการ Rich Menu ปัจจุบัน:")

    rich_menus = line_bot_api.get_rich_menu_list()

    if not rich_menus:
        print("- ไม่มี Rich Menu")
        return

    for menu in rich_menus:
        print(f"- {menu.rich_menu_id} | {menu.name} | {menu.chat_bar_text}")


# ==================================================
# Main
# ==================================================
if __name__ == "__main__":
    try:
        print("========================================")
        print("Reset + Create Two Rich Menus")
        print("========================================")

        # 1. เตรียมรูปทั้งสองเมนู
        prepare_image(REGISTER_SOURCE_IMAGE, REGISTER_OUTPUT_IMAGE)
        prepare_image(MAIN_SOURCE_IMAGE, MAIN_OUTPUT_IMAGE)

        # 2. ลบ Rich Menu เก่าทั้งหมด
        delete_all_rich_menus()

        # 3. สร้างเมนูลงทะเบียนใหม่
        register_menu_id = create_register_menu()

        # 4. สร้างเมนูหลักใหม่
        main_menu_id = create_main_menu()

        # 5. ตั้ง Default เป็นเมนูลงทะเบียน
        set_default_register_menu(register_menu_id)

        # 6. บันทึก ID ลง .env
        update_env_ids(register_menu_id, main_menu_id)

        # 7. แสดงรายการ
        list_rich_menus()

        print("\n========================================")
        print("สร้าง Rich Menu ทั้งสองแบบสำเร็จแล้ว")
        print(f"REGISTER_RICH_MENU_ID={register_menu_id}")
        print(f"MAIN_RICH_MENU_ID={main_menu_id}")
        print("========================================")

    except Exception as e:
        print("\nเกิดข้อผิดพลาด:")
        print(e)
        raise