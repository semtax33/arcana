import requests
import hashlib
import secrets
import pandas as pd
import re
import time

data = {
    'ID': [],
    'API_KEY': [],
}

# 1. Define your custom headers as a dictionary
headers = {
    "accept": "Bearer YOUR_TOKEN_HERE",
    "accept-language": "en-US;q=0.8,en;q=0.7,ar-IQ;q=0.6,ar-JO;q=0.5,ar;q=0.4,ja-JP;q=0.3,ja;q=0.2",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://www.alphavantage.co",
    "priority": "u=1, i ",
    "referer": "https://www.alphavantage.co/support/",
    "x-csrftoken": "Za8agSSdVkzcpZozo0KuoBZeuO6pbVcD",
    "x-requested-with": "XMLHttpRequest"
}

# 4. Define cookies
custom_cookies = {
    "csrftoken": "Za8agSSdVkzcpZozo0KuoBZeuO6pbVcD",
    "_ga": "GA1.1.1227698881.1784991645",
    "chatbase_anon_id":"52c3d91a-e595-43e6-ad6b-82548b7e03f3",
    "_gcl_au":"1.1.1497933193.1784991658",
    "_ga_FQEDGD32JV": "GS2.1.s1784991644 $o1 $g1 $t1784991657 $j47 $l0 $h0"
}

def generate_random_md5():
    # 1. Generate 32 secure random bytes
    random_bytes = secrets.token_bytes(32)
    
    # 2. Feed bytes into MD5 and return the hexadecimal string
    return hashlib.md5(random_bytes).hexdigest()

# Example Output: '7e5e01b3327d6d5ef634282c0b493e82'

for _ in range(0, 400):
    id = generate_random_md5()
    payload_body = {
        "first_text": "deprecated",
        "last_text": "deprecated",
        "occupation_text": "Investor",
        "organization_text": generate_random_md5(), 
        "email_text": f"{id}@proton.me"
    }
    response = requests.post(
        "https://www.alphavantage.co/create_post/", 
        headers=headers, 
        cookies=custom_cookies, 
        data=payload_body
    )
    response_data = response.json()
    token_text = response_data['text']
    token = re.findall(r'\b[A-Z0-9]{16}\b', token_text)[0]
    print(token)
    data['ID'].append(id)
    data['API_KEY'].append(token)
    time.sleep(2)

df = pd.DataFrame(data)
# Save to a CSV file without the row index numbers
df.to_csv('./scripts/token_output.csv', index=False)