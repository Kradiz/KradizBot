from dotenv import load_dotenv
from datetime import datetime

from google_sheets import append_user, get_assignments, get_latest_announcements

load_dotenv()

append_user(
    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "TEST_MODULE_ID",
    "ทดสอบ โมดูล",
    "2",
    "401",
    "student"
)

print("assignments:", get_assignments())
print("announcements:", get_latest_announcements())
print("ทดสอบ google_sheets.py สำเร็จ")