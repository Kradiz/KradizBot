import os
from dotenv import load_dotenv
from linebot import LineBotApi

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

if not CHANNEL_ACCESS_TOKEN:
    raise ValueError("ไม่พบ CHANNEL_ACCESS_TOKEN ในไฟล์ .env")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)

# ใส่ Rich Menu ID ตัวล่าสุดที่ต้องการเก็บไว้
KEEP_RICH_MENU_ID = "richmenu-d4204c36d206dbbf09a0abf6fc86cac8"

rich_menus = line_bot_api.get_rich_menu_list()

print("Rich Menu ทั้งหมด:")
for menu in rich_menus:
    print(f"- {menu.rich_menu_id} | {menu.name} | {menu.chat_bar_text}")

print("\nกำลังลบ Rich Menu เก่า...")

for menu in rich_menus:
    if menu.rich_menu_id == KEEP_RICH_MENU_ID:
        print(f"เก็บไว้: {menu.rich_menu_id}")
        continue

    try:
        print(f"ลบ: {menu.rich_menu_id} | {menu.name}")
        line_bot_api.delete_rich_menu(menu.rich_menu_id)
    except Exception as e:
        print(f"ลบไม่สำเร็จ: {menu.rich_menu_id} -> {e}")

print("\nเสร็จแล้ว")