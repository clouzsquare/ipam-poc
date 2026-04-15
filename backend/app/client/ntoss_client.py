import uuid
import random
import logging
from datetime import datetime
from functools import wraps

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("NTOSS_CLIENT")

def log_ntoss_call(func):
    """NTOSS API 호출 시 요청과 응답 로그를 남기는 데코레이터"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        func_name = func.__name__
        # 메서드 인자에서 self(args[0])를 제외한 값들 추출
        params = args[1:] if len(args) > 1 else kwargs
        
        logger.info(f"🚀 [NTOSS REQ] Calling: {func_name} | Args: {params}")
        
        try:
            result = func(*args, **kwargs)
            logger.info(f"✅ [NTOSS RES] {func_name} Success | Response: {result}")
            return result
        except Exception as e:
            logger.error(f"❌ [NTOSS ERR] {func_name} Failed | Error: {str(e)}")
            raise e
    return wrapper

class NtossClient:
    
    @log_ntoss_call
    def create_main_task(self, worker_id: str) -> dict:
        """메인 작업 생성 API (ID 생성 로직 유지)"""
        main_id = f"NTOSS-MAIN-{uuid.uuid4().hex[:6].upper()}"
        return {
            "main_job_id": main_id,
            "worker_id": worker_id,
            "created_at": datetime.now().isoformat()
        }

    @log_ntoss_call
    def create_sub_task(self, worker_id: str, main_id: str) -> dict:
        """서브 작업 생성 API"""
        sub_id = f"NTOSS-SUB-{uuid.uuid4().hex[:6].upper()}"
        return {
            "sub_job_id": sub_id,
            "main_job_id": main_id,
            "worker_id": worker_id,
            "status": "CREATED"
        }

    @log_ntoss_call
    def register_targets(self, sub_id: str, target_list: list) -> dict:
        """서브 작업에 대상 등록 API"""
        return {
            "result": "정상",
            "sub_job_id": sub_id,
            "registered_count": len(target_list),
            "timestamp": datetime.now().isoformat()
        }

    @log_ntoss_call
    def get_apartment_info_by_nw_id(self, nw_id: str) -> dict:
        """NW ID로 아파트 정보 조회 API (Mock)"""
        suffix = str(nw_id)[-1] if nw_id else ""
        mapping = {"1": "행복기숙사", "2": "시티호텔", "3": "중앙숙박업소"}
        name = mapping.get(suffix, "일반주거단지")
        return {
            "nw_id": nw_id,
            "apartment_name": name,
            "timestamp": datetime.now().isoformat(),
        }

    @log_ntoss_call
    def reclaim_dhcp(self, task_id: str, nw_id: str, ip: str) -> dict:
        """오전 11시: DHCP 서버 IP 회수 API (10% 확률 실패 로직 포함)"""
        is_success = random.random() > 0.1 
        return {
            "status": "SUCCESS" if is_success else "ERROR",
            "task_id": task_id,
            "nw_id": nw_id,
            "ip": ip,
            "error_msg": None if is_success else "DHCP Server Connection Timeout"
        }

    @log_ntoss_call
    def reclaim_device(self, task_id: str, nw_id: str, device_id: str, ip: str) -> dict:
        """오후 5시: 장비 IP 회수 API (5% 확률 실패 로직 포함)"""
        is_success = random.random() > 0.05
        return {
            "status": "SUCCESS" if is_success else "ERROR",
            "task_id": task_id,
            "nw_id": nw_id,
            "device_id": device_id,
            "ip": ip,
            "error_msg": None if is_success else "Device SNMP Response Error"
        }