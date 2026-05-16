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


# =========================
# โหลด .env
# =========================
load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
LIFF_REGISTER_URL = os.getenv("LIFF_REGISTER_URL")

if not CHANNEL_ACCESS_TOKEN:
    raise ValueError("ไม่พบ CHANNEL_ACCESS_TOKEN ในไฟล์ .env")

if not LIFF_REGISTER_URL:
    raise ValueError("ไม่พบ LIFF_REGISTER_URL ในไฟล์ .env")


line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)


# =========================
# ตั้งค่ารูป Rich Menu
# =========================
BASE_DIR = Path(__file__).resolve().parent

SOURCE_IMAGE = BASE_DIR / "richmenu_register.png"
RICHMENU_IMAGE = BASE_DIR / "richmenu_register_2500x1686.jpg"

RICHMENU_WIDTH = 2500
RICHMENU_HEIGHT = 1686


def prepare_richmenu_image():
    """
    เตรียมรูป Rich Menu ให้เป็น 2500x1686 jpg
    ถ้ามีไฟล์ richmenu_register_2500x1686.jpg อยู่แล้ว จะใช้เลย
    ถ้าไม่มี จะเอา richmenu_register.png มา resize/crop ให้
    """

    if RICHMENU_IMAGE.exists():
        print(f"พบรูปพร้อมใช้แล้ว: {RICHMENU_IMAGE.name}")
        return

    if not SOURCE_IMAGE.exists():
        raise FileNotFoundError(
            "ไม่พบไฟล์รูป Rich Menu\n"
            f"ต้องมีไฟล์อย่างน้อย 1 ไฟล์:\n"
            f"- {RICHMENU_IMAGE.name}\n"
            f"หรือ\n"
            f"- {SOURCE_IMAGE.name}"
        )

    print("กำลังปรับขนาดรูปเป็น 2500x1686...")

    img = Image.open(SOURCE_IMAGE).convert("RGB")

    src_w, src_h = img.size
    target_ratio = RICHMENU_WIDTH / RICHMENU_HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # รูปกว้างเกิน ตัดซ้ายขวา
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        # รูปสูงเกิน ตัดบนล่าง
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((RICHMENU_WIDTH, RICHMENU_HEIGHT), Image.LANCZOS)

    # LINE แนะนำให้รูปไม่เกิน 1 MB
    quality = 90
    while quality >= 60:
        img.save(RICHMENU_IMAGE, "JPEG", quality=quality, optimize=True)
        size_mb = RICHMENU_IMAGE.stat().st_size / (1024 * 1024)

        print(f"บันทึกรูป quality={quality}, ขนาด={size_mb:.2f} MB")

        if size_mb <= 1.0:
            break

        quality -= 5

    print(f"สร้างรูปสำเร็จ: {RICHMENU_IMAGE.name}")


def create_register_menu():
    """
    สร้าง Rich Menu ปุ่มเดียว กดแล้วเปิด LIFF ลงทะเบียน
    """

    print("กำลังสร้าง Rich Menu...")
    print(f"LIFF_REGISTER_URL = {LIFF_REGISTER_URL}")

    rich_menu = RichMenu(
        size=RichMenuSize(width=RICHMENU_WIDTH, height=RICHMENU_HEIGHT),
        selected=True,
        name="register_menu",
        chat_bar_text="เมนู",
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
    print(f"สร้าง Rich Menu ID สำเร็จ: {rich_menu_id}")

    print("กำลังอัปโหลดรูป Rich Menu...")
    with open(RICHMENU_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            rich_menu_id,
            "image/jpeg",
            f
        )

    print("อัปโหลดรูปสำเร็จ")

    print("กำลังตั้งเป็น Default Rich Menu...")
    line_bot_api.set_default_rich_menu(rich_menu_id)

    print("ตั้ง Default Rich Menu สำเร็จ")

    return rich_menu_id


def list_rich_menus():
    """
    แสดงรายการ Rich Menu ทั้งหมดในบัญชี
    """
    rich_menus = line_bot_api.get_rich_menu_list()

    print("\nรายการ Rich Menu ทั้งหมด:")
    if not rich_menus:
        print("- ไม่มี Rich Menu")
        return

    for menu in rich_menus:
        print(f"- {menu.rich_menu_id} | {menu.name} | {menu.chat_bar_text}")


def delete_all_rich_menus():
    """
    ถ้าต้องการลบ Rich Menu เก่าทั้งหมด ให้เปิดใช้ฟังก์ชันนี้ใน main
    """
    rich_menus = line_bot_api.get_rich_menu_list()

    if not rich_menus:
        print("ไม่มี Rich Menu เก่าให้ลบ")
        return

    print("กำลังลบ Rich Menu เก่าทั้งหมด...")

    for menu in rich_menus:
        try:
            print(f"ลบ: {menu.rich_menu_id} | {menu.name}")
            line_bot_api.delete_rich_menu(menu.rich_menu_id)
        except Exception as e:
            print(f"ลบไม่สำเร็จ: {menu.rich_menu_id} -> {e}")

    print("ลบ Rich Menu เก่าเสร็จแล้ว")


if __name__ == "__main__":
    try:
        prepare_richmenu_image()

        # ถ้าอยากลบเมนูเก่าทั้งหมดก่อนสร้างใหม่
        # ให้เอา # หน้าบรรทัดนี้ออก
        # delete_all_rich_menus()

        register_id = create_register_menu()

        print("\n==============================")
        print("สร้าง Rich Menu เสร็จแล้ว")
        print(f"REGISTER_RICH_MENU_ID={register_id}")
        print("==============================\n")

        list_rich_menus()

    except Exception as e:
        print("\nเกิดข้อผิดพลาด:")
        print(e)
        raise