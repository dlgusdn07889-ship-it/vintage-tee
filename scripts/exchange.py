import requests


def usd_to_krw(amount):
    try:
        url = "https://open.er-api.com/v6/latest/USD"

        response = requests.get(url, timeout=10)
        data = response.json()

        rate = data["rates"]["KRW"]
        krw = int(amount * rate)

        return {
            "rate": rate,
            "krw": krw
        }

    except Exception:
        # API 실패 시 임시 환율
        rate = 1480
        krw = int(amount * rate)

        return {
            "rate": rate,
            "krw": krw
        }


if __name__ == "__main__":
    print(usd_to_krw(120))
