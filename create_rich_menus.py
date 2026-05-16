from dotenv import load_dotenv
import os

from linebot import LineBotApi
from linebot.models import (
    RichMenu,
    RichMenuSize,
    RichMenuArea,
    RichMenuBounds,
    MessageAction
)

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

if not CHANNEL_ACCESS_TOKEN:
    raise Exception("ไม่พบ CHANNEL_ACCESS_TOKEN ในไฟล์ .env")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)


REGISTER_IMAGE = "richmenu_register_resized.jpg"
MAIN_IMAGE = "richmenu_main_resized.jpg"


def check_file(file_path):
    if not os.path.exists(file_path):
        raise Exception(f"ไม่พบไฟล์ {file_path}")

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(f"{file_path} size = {size_mb:.2f} MB")

    if size_mb > 1:
        raise Exception(f"{file_path} ใหญ่เกิน 1 MB กรุณารัน resize_richmenus.py ใหม่")


def create_register_menu():
    print("\nกำลังสร้าง Rich Menu: ลงทะเบียน")

    rich_menu = RichMenu(
        size=RichMenuSize(width=2500, height=1686),
        selected=True,
        name="register-menu",
        chat_bar_text="ลงทะเบียน",
        areas=[
            # ทั้งภาพเป็นปุ่มเดียว
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=0,
                    y=0,
                    width=2500,
                    height=1686
                ),
                action=MessageAction(
                    label="ลงทะเบียน",
                    text="ลงทะเบียน"
                )
            )
        ]
    )

    rich_menu_id = line_bot_api.create_rich_menu(rich_menu=rich_menu)
    print("สร้างเมนูลงทะเบียนแล้ว:", rich_menu_id)

    check_file(REGISTER_IMAGE)

    with open(REGISTER_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            rich_menu_id,
            "image/jpeg",
            f
        )

    print("อัปโหลดรูปเมนูลงทะเบียนสำเร็จ")
    return rich_menu_id


def create_main_menu():
    print("\nกำลังสร้าง Rich Menu: เมนูหลัก")

    rich_menu = RichMenu(
        size=RichMenuSize(width=2500, height=1686),
        selected=True,
        name="main-menu",
        chat_bar_text="เมนูระบบส่งงาน",
        areas=[
            # ซ้ายบน: ส่งงาน
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=0,
                    y=0,
                    width=1250,
                    height=843
                ),
                action=MessageAction(
                    label="ส่งงาน",
                    text="ส่งงาน"
                )
            ),

            # ขวาบน: ถามครูนัท
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=1250,
                    y=0,
                    width=1250,
                    height=843
                ),
                action=MessageAction(
                    label="ถามครูนัท",
                    text="ถามครูนัท"
                )
            ),

            # ซ้ายล่าง: งานค้าง
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=0,
                    y=843,
                    width=1250,
                    height=843
                ),
                action=MessageAction(
                    label="งานค้าง",
                    text="งานค้าง"
                )
            ),

            # ขวาล่าง: ประกาศจากครู
            RichMenuArea(
                bounds=RichMenuBounds(
                    x=1250,
                    y=843,
                    width=1250,
                    height=843
                ),
                action=MessageAction(
                    label="ประกาศจากครู",
                    text="ประกาศจากครู"
                )
            ),
        ]
    )

    rich_menu_id = line_bot_api.create_rich_menu(rich_menu=rich_menu)
    print("สร้างเมนูหลักแล้ว:", rich_menu_id)

    check_file(MAIN_IMAGE)

    with open(MAIN_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            rich_menu_id,
            "image/jpeg",
            f
        )

    print("อัปโหลดรูปเมนูหลักสำเร็จ")
    return rich_menu_id


def main():
    print("เริ่มสร้าง Rich Menu 2 ชุด")

    register_id = create_register_menu()
    main_id = create_main_menu()

    # ตั้ง default ให้คนใหม่เห็นเมนูลงทะเบียนก่อน
    line_bot_api.set_default_rich_menu(register_id)

    print("\n==============================")
    print("สร้าง Rich Menu สำเร็จ")
    print("==============================")
    print("REGISTER_RICH_MENU_ID=" + register_id)
    print("MAIN_RICH_MENU_ID=" + main_id)
    print("\nให้เอา 2 ค่านี้ไปใส่ใน .env และ Render Environment")


if __name__ == "__main__":
    main()