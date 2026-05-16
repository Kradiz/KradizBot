from dotenv import load_dotenv
import os
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

creds = Credentials.from_service_account_file(
    "credentials.json",
    scopes=SCOPES
)

client = gspread.authorize(creds)

sheet = client.open_by_key(GOOGLE_SHEET_ID)

ws = sheet.worksheet("users")

ws.append_row([
    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "TEST_LINE_ID",
    "ทดสอบ ระบบ",
    "1",
    "401",
    "student"
])

print("เขียน Google Sheets สำเร็จ")