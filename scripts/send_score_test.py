import os

import requests

from exchange import usd_to_krw


BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 테스트용 상품 정보
title = "1994 Nirvana Giant"
item_price = 65.0
us_shipping = 8.0
forwarding_fee = 10.0

# 배대지 비용 10달러를 총액에만 자동 포함
total_usd = item_price + us_shipping + forwarding_fee

item_price_krw = usd_to_krw(item_price)["krw"]
us_shipping_krw = usd_to_krw(us_shipping)["krw"]
total_krw = usd_to_krw(total_usd)["krw"]

message = f"""
🔥 빈티지 레이더

👕 상품명
{title}

💰 상품가
${item_price:.2f} / 약 {item_price_krw:,}원

🚚 미국 배송비
${us_shipping:.2f} / 약 {us_shipping_krw:,}원

💵 총 예상금액
${total_usd:.2f} / 약 {total_krw:,}원
배대지 비용 $10 포함

🏷 태그
Giant 40점

📅 연식
1994년 20점

🧵 봉제 방식
싱글스티치 15점

🌎 생산국
미국 10점

⭐ 빈티지 점수
85 / 100

🏆 등급
S

🤖 평가
즉시 검토 추천
"""

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

response = requests.post(
    url,
    data={
        "chat_id": CHAT_ID,
        "text": message,
    },
    timeout=20,
)

response.raise_for_status()
print("Telegram message sent successfully.")
