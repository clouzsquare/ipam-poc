import os
import logging
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.database import SessionLocal
from app.repositories.reclaim_job.job_repository import JobRepository
from app.client.ntoss_client import NtossClient
from app.utils.gmail_service import send_error_notification

router = APIRouter()
logger = logging.getLogger("SCHEDULER")
ntoss = NtossClient()


@router.post("/scheduler/dhcp")
def run_dhcp_reclaim():
    """
    [11:00 스케줄] DHCP 서버 IP 회수
    - IN-PROGRESS 상태 아이템 대상으로 NTOSS DHCP 회수 호출
    - 성공: DHCP_SUCCESS
    - 실패: DHCP_FAILED + 작업없음 처리 + 관리자 오류 메일
    """
    db = SessionLocal()
    try:
        repo = JobRepository(db)
        job = repo.get_active_job()
        if not job:
            return {"message": "진행 중인 활성 작업이 없습니다."}

        items = repo.get_items_by_job_and_status(job.ip_reclaim_job_id, ["IN-PROGRESS"])
        if not items:
            return {"message": "DHCP 회수 대상 아이템이 없습니다. (IN-PROGRESS 상태 없음)"}

        results = []
        for item in items:
            res = ntoss.reclaim_dhcp(job.sub_task_id, item.nw_id, item.ip_address)

            if res["status"] == "SUCCESS":
                repo.update_item_status_by_id(
                    item.ip_reclaim_job_item_id,
                    "DHCP_SUCCESS",
                    dhcp_result="SUCCESS"
                )
                results.append({"ip": item.ip_address, "status": "DHCP_SUCCESS"})
                logger.info(f"[DHCP] SUCCESS: {item.ip_address}")
            else:
                error_msg = res.get("error_msg", "Unknown error")
                # 작업없음 처리
                ntoss.no_action(job.sub_task_id, item.nw_id, item.ip_address)
                repo.update_item_status_by_id(
                    item.ip_reclaim_job_item_id,
                    "DHCP_FAILED",
                    dhcp_result="FAILED",
                    ntoss_result_message=error_msg
                )
                # 관리자 오류 메일
                admin_email = os.getenv("GMAIL_USER", "admin@example.com")
                send_error_notification(admin_email, "DHCP", item.ip_address, item.nw_id, error_msg)
                results.append({"ip": item.ip_address, "status": "DHCP_FAILED", "error": error_msg})
                logger.warning(f"[DHCP] FAILED: {item.ip_address} | {error_msg}")

        return {"job_id": job.ip_reclaim_job_id, "processed": len(items), "results": results}
    finally:
        db.close()


@router.post("/scheduler/device")
def run_device_reclaim():
    """
    [17:00 스케줄] 장비 IP 회수
    - DHCP_SUCCESS 상태 아이템 대상으로 NTOSS 장비 회수 호출
    - 성공: DEVICE_SUCCESS → 전체 성공 시 서브/메인 작업 완료 처리
    - 실패: DEVICE_FAILED + 신규 서브작업 생성 + IP 재할당 + 서브작업 완료 + 작업없음 + 관리자 메일
    """
    db = SessionLocal()
    try:
        repo = JobRepository(db)
        job = repo.get_active_job()
        if not job:
            return {"message": "진행 중인 활성 작업이 없습니다."}

        items = repo.get_items_by_job_and_status(job.ip_reclaim_job_id, ["DHCP_SUCCESS"])
        if not items:
            return {"message": "장비 회수 대상 아이템이 없습니다. (DHCP_SUCCESS 상태 없음)"}

        results = []
        has_failure = False

        for item in items:
            device_id = item.device_id or "DEVICE-UNKNOWN"
            res = ntoss.reclaim_device(job.sub_task_id, item.nw_id, device_id, item.ip_address)

            if res["status"] == "SUCCESS":
                repo.update_item_status_by_id(
                    item.ip_reclaim_job_item_id,
                    "DEVICE_SUCCESS",
                    device_result="SUCCESS"
                )
                results.append({"ip": item.ip_address, "status": "DEVICE_SUCCESS"})
                logger.info(f"[DEVICE] SUCCESS: {item.ip_address}")
            else:
                has_failure = True
                error_msg = res.get("error_msg", "Unknown error")

                # 1) 신규 서브작업 생성 (원복용)
                new_sub = ntoss.create_sub_task("ADMIN_DONGHYUK", job.main_task_id)
                # 2) IP 할당(원복)
                ntoss.allocate_ip(new_sub["sub_job_id"], item.ip_address)
                # 3) 원복 서브작업 완료
                ntoss.complete_sub_task(new_sub["sub_job_id"])
                # 4) 기존 서브작업 작업없음 처리
                ntoss.no_action(job.sub_task_id, item.nw_id, item.ip_address)

                repo.update_item_status_by_id(
                    item.ip_reclaim_job_item_id,
                    "DEVICE_FAILED",
                    device_result="FAILED",
                    ntoss_result_message=error_msg
                )
                # 관리자 오류 메일
                admin_email = os.getenv("GMAIL_USER", "admin@example.com")
                send_error_notification(admin_email, "장비", item.ip_address, item.nw_id, error_msg)
                results.append({"ip": item.ip_address, "status": "DEVICE_FAILED", "error": error_msg})
                logger.warning(f"[DEVICE] FAILED: {item.ip_address} | {error_msg}")

        # 실패 없을 때만 서브/메인 작업 완료 처리
        if not has_failure and items:
            ntoss.complete_sub_task(job.sub_task_id)
            ntoss.complete_main_task(job.main_task_id)
            repo.update_job_status(job.ip_reclaim_job_id, "DONE")
            logger.info(f"[DEVICE] 전체 완료 - job_id: {job.ip_reclaim_job_id}")

        return {"job_id": job.ip_reclaim_job_id, "processed": len(items), "results": results}
    finally:
        db.close()


class MailReplyRequest(BaseModel):
    ip_address: str
    action: str  # APPROVE | REJECT


@router.post("/scheduler/mail-reply")
def handle_mail_reply(req: MailReplyRequest):
    """
    [Mock] 담당자 메일 회신 처리
    - APPROVE: 승인 (기존 IN-PROGRESS 유지, DHCP 회수 예정)
    - REJECT: 회수 대상 제외 (REJECTED 상태 변경)
    """
    if req.action.upper() == "REJECT":
        db = SessionLocal()
        try:
            repo = JobRepository(db)
            filters = [{"target": "ip_address", "value": [req.ip_address]}]
            count = repo.bulk_update_item_status_by_filters(filters, "REJECTED")
            return {"message": f"{req.ip_address} 회수 제외 처리 완료", "updated": count}
        finally:
            db.close()
    else:
        return {"message": f"{req.ip_address} 승인 처리 완료. 11:00 DHCP 회수 스케줄에 포함됩니다."}
