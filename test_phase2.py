import requests

url = "http://localhost:8000/chat"
payload = {"message": "テストです。プロジェクトで必要なものは何ですか？"}
headers = {"Content-Type": "application/json"}

try:
    response = requests.post(url, json=payload, headers=headers)
    print("Status Code:", response.status_code)
    print("Response JSON:")
    print(response.json())
except Exception as e:
    print("Error:", e)
