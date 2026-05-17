from pathlib import Path
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
RICHMENU_DIR = BASE_DIR / "richmenus"

FILES = [
    "richmenu_teacher_setup.png",
    "richmenu_teacher_normal.png",
    "richmenu_teacher_question_alert.png",

    "richmenu_student_register.png",
    "richmenu_student_normal.png",
    "richmenu_student_pending_alert.png",
    "richmenu_student_answer_alert.png",
    "richmenu_student_both_alert.png",
]

MAX_WIDTH = 2500
MAX_HEIGHT = 1686

for filename in FILES:
    path = RICHMENU_DIR / filename

    if not path.exists():
        print(f"[SKIP] missing: {filename}")
        continue

    img = Image.open(path).convert("RGB")

    # บังคับขนาดให้ตรงกับ Rich Menu
    img = img.resize((MAX_WIDTH, MAX_HEIGHT), Image.LANCZOS)

    # เซฟเป็น PNG แบบ optimize
    img.save(path, format="PNG", optimize=True, compress_level=9)

    size_mb = path.stat().st_size / 1024 / 1024
    print(f"[OK] {filename}: {size_mb:.2f} MB")

print("Done")