from PIL import Image
from pathlib import Path

# เปลี่ยนชื่อไฟล์นี้ให้ตรงกับรูปของคุณ
input_path = Path("richmenu_register.png")
output_path = Path("richmenu_register_2500x1686.jpg")

target_w, target_h = 2500, 1686

img = Image.open(input_path).convert("RGB")

# crop แบบ center ให้สัดส่วนตรงกับ 2500x1686
src_w, src_h = img.size
target_ratio = target_w / target_h
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

img = img.resize((target_w, target_h), Image.LANCZOS)

# บีบอัดให้ไฟล์ไม่ใหญ่เกินไป
quality = 90
while quality >= 60:
    img.save(output_path, "JPEG", quality=quality, optimize=True)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"saved quality={quality}, size={size_mb:.2f} MB")
    if size_mb <= 1.0:
        break
    quality -= 5

print("เสร็จแล้ว:", output_path)