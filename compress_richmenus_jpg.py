from pathlib import Path
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
RICHMENU_DIR = BASE_DIR / "richmenus"

FILES = [
    "richmenu_teacher_setup",
    "richmenu_teacher_normal",
    "richmenu_teacher_question_alert",

    "richmenu_student_register",
    "richmenu_student_normal",
    "richmenu_student_pending_alert",

    "richmenu_student_answer_alert",
    "richmenu_student_both_alert",
]

WIDTH = 2500
HEIGHT = 1686

# ถ้าไฟล์ยังใหญ่เกิน 1 MB ให้ลดเป็น 70 หรือ 60
QUALITY = 70

for name in FILES:
    png_path = RICHMENU_DIR / f"{name}.png"
    jpg_path = RICHMENU_DIR / f"{name}.jpg"

    if not png_path.exists():
        print(f"[SKIP] missing: {png_path.name}")
        continue

    img = Image.open(png_path).convert("RGB")
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)

    img.save(
        jpg_path,
        format="JPEG",
        quality=QUALITY,
        optimize=True,
        progressive=True
    )

    size_mb = jpg_path.stat().st_size / 1024 / 1024
    print(f"[OK] {jpg_path.name}: {size_mb:.2f} MB")

print("Done")