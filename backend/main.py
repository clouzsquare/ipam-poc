import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app.api.v1.chat import router as chat_router
from app.client.ntoss_client import NtossClient
import logging

# 로그
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("LOGGER")

load_dotenv()

app = FastAPI(
    title="IPAM AI Agent PoC",
    description="LG CNS NW AX IPAM 프로젝트 - 멀티 에이전트 오케스트레이션 시스템"
)

# NTOSS 클라이언트 공통 사용을 위해 인스턴스화 (필요 시)
ntoss = NtossClient()

# CORS 설정: 프론트엔드(React)와의 통신을 위해 유지
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 라우터 등록
# /api/v1/chat 으로 들어오는 요청은 이제 Master Router가 처리합니다.
app.include_router(chat_router, prefix="/api/v1", tags=["Chat"])

@app.get("/")
async def root():
    return {"message": "IPAM AI Agent PoC Server is running."}

# Gmail 발송 유틸리티 (필요 시 별도 mail_service.py로 분리 추천)
def send_gmail(subject: str, body: str, to_email: str):
    import smtplib
    from email.mime.text import MIMEText
    
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    
    if not gmail_user or not gmail_password:
        print(f"[MAIL MOCK] To: {to_email}, Subject: {subject}")
        return

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = to_email

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(gmail_user, gmail_password)
            smtp.send_message(msg)
        print(f"[MAIL SENT] To: {to_email}")
    except Exception as e:
        print(f"[MAIL ERROR] {str(e)}")

if __name__ == "__main__":
    # 로컬 개발 시 reload=True 옵션으로 코드 수정 시 자동 반영
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)