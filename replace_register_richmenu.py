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
)


# ==================================================
# โหลดค่าจาก .env
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
# ตั้งค่าไฟล์รูป
# ==================================================
BASE_DIR = Path(__file__).resolve().parent

SOURCE_IMAGE = BASE_DIR / "richmenu_register.png"
OUTPUT_IMAGE = BASE_DIR / "richmenu_register_2500x1686.jpg"

RICHMENU_WIDTH = 2500
RICHMENU_HEIGHT = 1686


# ==================================================
# ตั้งค่า Rich Menu
# ==================================================
RICHMENU_NAME = "register-menu"
CHAT_BAR_TEXT = "ลงทะเบียน"


def prepare_richmenu_image():
    """
    แปลงรูป richmenu_register.png เป็น 2500x1686 jpg
    และบีบอัดให้ไม่เกิน 1 MB
    """

    if not SOURCE_IMAGE.exists():
        raise FileNotFoundError(
            f"ไม่พบไฟล์รูป {SOURCE_IMAGE.name}\n"
            f"กรุณาวางไฟล์ไว้ที่:\n{SOURCE_IMAGE}"
        )

    print("กำลังเตรียมรูป Rich Menu...")
    print(f"ไฟล์ต้นฉบับ: {SOURCE_IMAGE}")

    img = Image.open(SOURCE_IMAGE).convert("RGB")

    src_w, src_h = img.size
    print(f"ขนาดรูปต้นฉบับ: {src_w} x {src_h}")

    target_ratio = RICHMENU_WIDTH / RICHMENU_HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # รูปกว้างเกิน ตัดซ้ายขวา
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
        print("ตัดรูปด้านซ้าย/ขวาให้ตรงสัดส่วน")
    else:
        # รูปสูงเกิน ตัดบนล่าง
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))
        print("ตัดรูปด้านบน/ล่างให้ตรงสัดส่วน")

    img = img.resize((RICHMENU_WIDTH, RICHMENU_HEIGHT), Image.LANCZOS)

    quality = 90

    while quality >= 50:
        img.save(OUTPUT_IMAGE, "JPEG", quality=quality, optimize=True)
        size_mb = OUTPUT_IMAGE.stat().st_size / (1024 * 1024)
        print(f"บันทึก quality={quality}, ขนาดไฟล์={size_mb:.2f} MB")

        if size_mb <= 1.0:
            break

        quality -= 5

    if OUTPUT_IMAGE.stat().st_size > 1024 * 1024:
        raise ValueError(
            "ไฟล์รูปยังเกิน 1 MB แม้ลด quality แล้ว\n"
            "แนะนำให้ใช้รูปพื้นหลังเรียบขึ้น หรือบีบอัดรูปก่อน"
        )

    print(f"เตรียมรูปสำเร็จ: {OUTPUT_IMAGE.name}")


def create_new_register_richmenu():
    """
    สร้าง Rich Menu ลงทะเบียนใหม่
    ทั้งภาพเป็นปุ่มเดียว กดแล้วเปิด LIFF_REGISTER_URL
    """

    print("\nกำลังสร้าง Rich Menu ลงทะเบียนใหม่...")
    print(f"LIFF_REGISTER_URL = {LIFF_REGISTER_URL}")

    rich_menu = RichMenu(
        size=RichMenuSize(
            width=RICHMENU_WIDTH,
            height=RICHMENU_HEIGHT,
        ),
        selected=True,
        name=RICHMENU_NAME,
        chat_bar_text=CHAT_BAR_TEXT,
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

    print(f"สร้าง Rich Menu สำเร็จ:")
    print(rich_menu_id)

    print("กำลังอัปโหลดรูป Rich Menu...")
    with open(OUTPUT_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            rich_menu_id,
            "image/jpeg",
            f,
        )

    print("อัปโหลดรูปสำเร็จ")

    print("กำลังตั้งเป็น Default Rich Menu...")
    line_bot_api.set_default_rich_menu(rich_menu_id)
    print("ตั้งเป็น Default Rich Menu สำเร็จ")

    return rich_menu_id


def list_rich_menus():
    """
    ดึงรายการ Rich Menu ทั้งหมด
    """
    return line_bot_api.get_rich_menu_list()


def delete_old_register_richmenus(keep_rich_menu_id):
    """
    ลบ Rich Menu เก่าออก
    - เก็บตัวใหม่ไว้
    - ไม่ลบเมนูที่ชื่อมีคำว่า main เพื่อกันลบ main-menu
    """

    print("\nกำลังตรวจสอบ Rich Menu เก่าทั้งหมด...")

    rich_menus = list_rich_menus()

    if not rich_menus:
        print("ไม่พบ Rich Menu อื่น")
        return

    for menu in rich_menus:
        menu_id = menu.rich_menu_id
        menu_name = menu.name or ""
        chat_bar_text = menu.chat_bar_text or ""

        print(f"- {menu_id} | {menu_name} | {chat_bar_text}")

    print("\nกำลังลบ Rich Menu เก่าที่เป็นเมนูลงทะเบียน...")

    for menu in rich_menus:
        menu_id = menu.rich_menu_id
        menu_name = (menu.name or "").lower()
        chat_bar_text = menu.chat_bar_text or ""

        if menu_id == keep_rich_menu_id:
            print(f"เก็บตัวใหม่ไว้: {menu_id}")
            continue

        # กันพลาด ไม่ลบ main menu
        if "main" in menu_name:
            print(f"ข้าม main-menu: {menu_id} | {menu.name}")
            continue

        # ลบเฉพาะเมนูที่น่าจะเป็น register menu
        is_register_menu = (
            "register" in menu_name
            or "ลงทะเบียน" in chat_bar_text
            or "register" in chat_bar_text.lower()
        )

        if not is_register_menu:
            print(f"ข้ามเมนูอื่น: {menu_id} | {menu.name}")
            continue

        try:
            print(f"ลบเมนูเก่า: {menu_id} | {menu.name}")
            line_bot_api.delete_rich_menu(menu_id)
        except Exception as e:
            print(f"ลบไม่สำเร็จ: {menu_id} -> {e}")

    print("ลบ Rich Menu เก่าเสร็จแล้ว")


def show_final_richmenus():
    """
    แสดงรายการ Rich Menu หลังทำงานเสร็จ
    """
    print("\nรายการ Rich Menu หลังอัปเดต:")

    rich_menus = list_rich_menus()

    if not rich_menus:
        print("- ไม่มี Rich Menu")
        return

    for menu in rich_menus:
        print(f"- {menu.rich_menu_id} | {menu.name} | {menu.chat_bar_text}")


if __name__ == "__main__":
    try:
        print("==============================")
        print("Replace Register Rich Menu")
        print("==============================")

        prepare_richmenu_image()

        new_rich_menu_id = create_new_register_richmenu()

        delete_old_register_richmenus(new_rich_menu_id)

        show_final_richmenus()

        print("\n==============================")
        print("เปลี่ยน Rich Menu ลงทะเบียนสำเร็จแล้ว")
        print(f"REGISTER_RICH_MENU_ID={new_rich_menu_id}")
        print("==============================")

    except Exception as e:
        print("\nเกิดข้อผิดพลาด:")
        print(e)
        raise