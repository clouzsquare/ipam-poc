import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger("GMAIL_SERVICE")


def send_mail(to_email: str, subject: str, body: str) -> bool:
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_password:
        logger.info(f"[MAIL MOCK] To: {to_email} | Subject: {subject}\n{body}")
        return True

    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = gmail_user
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, gmail_password)
            smtp.send_message(msg)

        logger.info(f"[MAIL SENT] To: {to_email}")
        return True
    except Exception as e:
        logger.error(f"[MAIL ERROR] To: {to_email} | {str(e)}")
        return False


def send_reclaim_notification(owner_email: str, ip_address: str, nw_id: str, owner_team: str) -> bool:
    """IP 회수 사전 안내 메일 발송"""
    subject = f"[IPAM] IP 회수 작업 사전 안내 - {ip_address}"
    body = f"""안녕하세요,

IPAM 시스템에서 아래 IP에 대한 회수 작업이 금일 예정되어 있습니다.

- IP 주소: {ip_address}
- NW ID: {nw_id}
- 담당 팀: {owner_team}

이의가 있으신 경우 이 메일에 회신해 주세요.
회신이 없을 경우 승인으로 처리됩니다.

감사합니다.
IPAM AI Assistant
"""
    return send_mail(owner_email, subject, body)


def send_error_notification(admin_email: str, error_type: str, ip_address: str, nw_id: str, error_msg: str) -> bool:
    """회수 오류 알림 메일 발송 (관리자용)"""
    subject = f"[IPAM 오류] {error_type} 회수 실패 - {ip_address}"
    body = f"""IPAM 시스템에서 오류가 발생했습니다.

- 오류 유형: {error_type}
- IP 주소: {ip_address}
- NW ID: {nw_id}
- 오류 메시지: {error_msg}

조치 진행 여부를 회신해 주세요.

IPAM AI Assistant
"""
    return send_mail(admin_email, subject, body)
