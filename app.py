import os
import traceback
from flask import Flask, request, abort
from dotenv import load_dotenv
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

# โหลดค่าจาก .env
load_dotenv()

app = Flask(__name__)

# LINE API
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# OpenAI Client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.route("/")
def home():
    return "✅ LINE Bot พร้อมใช้งาน!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    print("📨 ข้อความจากผู้ใช้:", user_msg)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # หรือ gpt-3.5-turbo
            messages=[
                {"role": "system", "content": "คุณคือผู้ช่วยวิเคราะห์หุ้นสำหรับคนไม่มีความรู้เรื่องการลงทุน"},
                {"role": "user", "content": user_msg}
            ]
        )
        reply_text = response.choices[0].message.content
        print("🤖 คำตอบจาก GPT:", reply_text)

    except Exception as e:
        print("❌ เกิดข้อผิดพลาดจาก OpenAI:")
        traceback.print_exc()
        reply_text = "ขออภัย ระบบวิเคราะห์ผิดพลาด กรุณาลองใหม่ภายหลัง"

    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        print("❌ เกิดข้อผิดพลาดในการตอบกลับ LINE:")
        traceback.print_exc()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
