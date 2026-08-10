import hashlib
import os
import re
import secrets
import time
from pathlib import Path

import pandas as pd
import requests

ALPHA_VANTAGE_CSRF_TOKEN_ENV = "ALPHA_VANTAGE_CSRF_TOKEN"
OUTPUT_PATH = Path(__file__).with_name("token_output.csv")


def generate_random_md5():
    # 1. Generate 32 secure random bytes
    random_bytes = secrets.token_bytes(32)
    
    # 2. Feed bytes into MD5 and return the hexadecimal string
    return hashlib.md5(random_bytes).hexdigest()


def collect_api_keys(count=400, delay_seconds=2):
    csrf_token = os.getenv(ALPHA_VANTAGE_CSRF_TOKEN_ENV, "").strip()
    if not csrf_token:
        raise ValueError(f"{ALPHA_VANTAGE_CSRF_TOKEN_ENV} environment variable is required")

    headers = {
        "accept-language": "en-US;q=0.8,en;q=0.7",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.alphavantage.co",
        "referer": "https://www.alphavantage.co/support/",
        "x-csrftoken": csrf_token,
        "x-requested-with": "XMLHttpRequest",
    }
    cookies = {"csrftoken": csrf_token}
    data = {"ID": [], "API_KEY": []}

    for index in range(count):
        identifier = generate_random_md5()
        payload_body = {
            "first_text": "deprecated",
            "last_text": "deprecated",
            "occupation_text": "Investor",
            "organization_text": generate_random_md5(),
            "email_text": f"{identifier}@proton.me",
        }
        response = requests.post(
            "https://www.alphavantage.co/create_post/",
            headers=headers,
            cookies=cookies,
            data=payload_body,
        )
        response.raise_for_status()
        matches = re.findall(r"\b[A-Z0-9]{16}\b", response.json()["text"])
        if not matches:
            raise ValueError("Alpha Vantage response did not contain an API key")
        data["ID"].append(identifier)
        data["API_KEY"].append(matches[0])
        print(f"Collected API key {index + 1}/{count}")
        time.sleep(delay_seconds)

    return pd.DataFrame(data)


def main():
    collect_api_keys().to_csv(OUTPUT_PATH, index=False)


if __name__ == "__main__":
    main()
