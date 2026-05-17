import os
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

from linebot import LineBotApi


# =========================
# โหลด .env
# =========================
load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

if not CHANNEL_ACCESS_TOKEN:
    raise ValueError("ไม่พบ CHANNEL_ACCESS_TOKEN ในไฟล์ .env")


# =========================
# ตั้งค่า
# =========================
BASE_DIR = Path(__file__).resolve().parent

# รูปต้นฉบับใหม่ที่ต้องการใช้
SOURCE_IMAGE = BASE_DIR / "richmenu_register.png"

# รูปที่ระบบจะแปลงให้พร้อมอัปโหลด
OUTPUT_IMAGE = BASE_DIR / "richmenu_register_2500x1686.jpg"

# Rich Menu ID เดิมที่ต้องการเปลี่ยนรูป
RICH_MENU_ID = "richmenu-d4204c36d206dbbf09a0abf6fc86cac8"

WIDTH = 2500
HEIGHT = 1686


line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)


def resize_image():
    """
    แปลงรูป richmenu_register.png เป็น 2500x1686 jpg
    และบีบอัดให้ไม่เกิน 1 MB
    """

    if not SOURCE_IMAGE.exists():
        raise FileNotFoundError(
            f"ไม่พบไฟล์ {SOURCE_IMAGE.name}\n"
            f"กรุณาวางไฟล์รูปใหม่ไว้ที่:\n{SOURCE_IMAGE}"
        )

    print(f"พบรูปต้นฉบับ: {SOURCE_IMAGE.name}")

    img = Image.open(SOURCE_IMAGE).convert("RGB")

    src_w, src_h = img.size
    print(f"ขนาดรูปต้นฉบับ: {src_w} x {src_h}")

    target_ratio = WIDTH / HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # รูปกว้างเกิน ตัดซ้ายขวา
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
        print("ตัดรูปด้านซ้าย/ขวาให้ได้สัดส่วน")
    else:
        # รูปสูงเกิน ตัดบนล่าง
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))
        print("ตัดรูปด้านบน/ล่างให้ได้สัดส่วน")

    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)

    quality = 90

    while quality >= 50:
        img.save(OUTPUT_IMAGE, "JPEG", quality=quality, optimize=True)
        size_mb = OUTPUT_IMAGE.stat().st_size / (1024 * 1024)

        print(f"บันทึก quality={quality}, size={size_mb:.2f} MB")

        if size_mb <= 1.0:
            break

        quality -= 5

    if OUTPUT_IMAGE.stat().st_size > 1024 * 1024:
        raise ValueError(
            "ไฟล์รูปยังเกิน 1 MB แม้ลด quality แล้ว\n"
            "ให้ลดรายละเอียดรูป หรือใช้พื้นหลังเรียบขึ้น"
        )

    print(f"เตรียมรูปสำเร็จ: {OUTPUT_IMAGE.name}")


def update_richmenu_image():
    """
    อัปโหลดรูปใหม่ทับ Rich Menu เดิม
    """

    print("กำลังอัปเดตรูป Rich Menu ID:")
    print(RICH_MENU_ID)

    with open(OUTPUT_IMAGE, "rb") as f:
        line_bot_api.set_rich_menu_image(
            RICH_MENU_ID,
            "image/jpeg",
            f
        )

    print("อัปเดตรูป Rich Menu สำเร็จ")


if __name__ == "__main__":
    try:
        resize_image()
        update_richmenu_image()

        print("\n==============================")
        print("เปลี่ยนรูป Rich Menu สำเร็จแล้ว")
        print("==============================")

    except Exception as e:
        print("\nเกิดข้อผิดพลาด:")
        print(e)
        raise