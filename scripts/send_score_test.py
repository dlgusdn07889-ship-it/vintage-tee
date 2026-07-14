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

# 배대지 비용 10달러는 총액에만 포함
total_usd = item_price + us_shipping + forwarding_fee

# 환율은 한 번만 불러오고 같은 환율로 전부 계산
exchange_result = usd_to_krw(1)
exchange_rate = exchange_result["rate"]

item_price_krw = round(item_price * exchange_rate)
us_shipping_krw = round(us_shipping * exchange_rate)
total_krw = round(total_usd * exchange_rate)

message = f"""
🎯 빈티지 레이더

👕 {title}

💰 상품가
${item_price:.2f} / 약 {item_price_krw:,}원

🚚 미국 배송비
${us_shipping:.2f} / 약 {us_shipping_krw:,}원

💵 총 예상금액
${total_usd:.2f} / 약 {total_krw:,}원
(배대지 $10 포함)

⭐ 추천도
★★★★★

🤖 추천
즉시 검토 추천
""".strip()

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
print("텔레그램 메시지 전송 성공")
