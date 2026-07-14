import os
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

message = """
🔥 Vintage Intelligence

1994 Nirvana Giant

⭐ Score : 92 / 100

🏷 Tag : Giant
🧵 Single Stitch
🇺🇸 Made in USA

💰 Price : $65

🤖 AI Recommendation
★★★★★
BUY NOW
"""

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

requests.post(
    url,
    data={
        "chat_id": CHAT_ID,
        "text": message
    }
)
