import os
import requests

CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]


def get_access_token():
    url = "https://api.ebay.com/identity/v1/oauth2/token"

    response = requests.post(
        url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=20,
    )

    response.raise_for_status()

    token = response.json()["access_token"]

    return token


if __name__ == "__main__":
    token = get_access_token()

    print("✅ eBay API 연결 성공!")
    print(token[:30] + "...")
