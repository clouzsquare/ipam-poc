import requests
import json
import time

BASE_URL = "http://localhost:8000/api/v1"

def test_chat_flow():
    print("--- Testing Chat Flow ---")
    
    # 1. Start Reclaim
    payload = {
        "history": [{"role": "user", "content": "금일 IP 회수작업 진행 요청해줘"}]
    }
    res = requests.post(f"{BASE_URL}/chat", json=payload).json()
    print("Assistant:", res["content"])
    
    max_per_team = res.get("max_per_team")
    selected_ips = res.get("selected_ips")
    
    # 2. Confirm
    payload = {
        "history": [
            {"role": "user", "content": "금일 IP 회수작업 진행 요청해줘"},
            {"role": "assistant", "content": res["content"]},
            {"role": "user", "content": "확정 및 진행 요청"}
        ],
        "max_per_team": max_per_team,
        "selected_ips": selected_ips
    }
    res = requests.post(f"{BASE_URL}/chat", json=payload).json()
    print("Assistant:", res["content"])

def test_scheduled_tasks():
    print("\n--- Testing Scheduled Tasks ---")
    
    # 1. 11am DHCP Reclaim
    print("Running 11am schedule...")
    res = requests.post(f"{BASE_URL}/scheduler/dhcp").json()
    print("Result:", json.dumps(res, indent=2, ensure_ascii=False))
    
    # 2. 5pm Device Reclaim
    print("\nRunning 5pm schedule...")
    res = requests.post(f"{BASE_URL}/scheduler/device").json()
    print("Result:", json.dumps(res, indent=2, ensure_ascii=False))

def test_status_query():
    print("\n--- Testing Status Query ---")
    payload = {
        "history": [{"role": "user", "content": "현재 진행 상황 알려줘"}]
    }
    res = requests.post(f"{BASE_URL}/chat", json=payload).json()
    print("Assistant:", res["content"])

if __name__ == "__main__":
    # Start the server in background first if not running
    # For this test, I'll assume I can run it manually or I'll just check the code logic.
    # Since I cannot easily run the server and test it in the same turn without backgrounding,
    # I will just execute the functions if the server is up.
    try:
        test_chat_flow()
        test_scheduled_tasks()
        test_status_query()
    except Exception as e:
        print("Error connecting to server. Make sure main.py is running.")
        print(e)
