from sqlalchemy.orm import Session
from sqlalchemy import desc, and_
from app.models.entities import IpReclaimJob, IpReclaimJobItem, IpReclaimCandidate

class JobRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_jobs_by_filter(self, job_id: str = None, owner_team: str = None, item_status: str = None, limit: int = 50):
        """
        사용자 의도(Query Plan)에 따라 동적으로 작업 현황을 조회합니다.
        """
        # JobItem과 Job을 조인하여 팀 정보와 작업 ID를 동시에 필터링할 수 있게 합니다.
        query = self.db.query(IpReclaimJobItem).join(
            IpReclaimJob, IpReclaimJobItem.ip_reclaim_job_id == IpReclaimJob.ip_reclaim_job_id
        )
        
        filters = []
        
        # 1. 특정 작업 ID로 조회 (예: "NTOSS-MAIN-123 번 작업 어때?")
        if job_id:
            filters.append(IpReclaimJob.main_task_id == job_id)
            
        # 2. 특정 팀으로 조회 (예: "인프라팀 진행 상황 알려줘")
        if owner_team:
            filters.append(IpReclaimJobItem.owner_team == owner_team)
            
        # 3. 특정 상태로 조회 (예: "실패한 것들만 보여줘")
        if item_status:
            filters.append(IpReclaimJobItem.item_status == item_status)
            
        # 필터 적용
        if filters:
            query = query.filter(and_(*filters))
            
        # 최신 순으로 정렬하여 반환
        return query.order_by(desc(IpReclaimJobItem.created_at)).limit(limit).all()

    def get_latest_job_summary(self):
        """
        가장 최근에 생성된 작업의 전체 요약 정보를 가져옵니다.
        (사용자가 단순히 '진행 현황 알려줘'라고 했을 때 사용)
        """
        latest_job = self.db.query(IpReclaimJob).order_by(desc(IpReclaimJob.ip_reclaim_job_id)).first()
        if not latest_job:
            return None
            
        items = self.db.query(IpReclaimJobItem).filter(
            IpReclaimJobItem.ip_reclaim_job_id == latest_job.ip_reclaim_job_id
        ).all()
        
        return {
            "job_info": latest_job,
            "items": items
        }

    def create_reclaim_job(self, main_task_id: str, sub_task_id: str, requester_id: str, selected_ips: list):
        """
        확정된 리스트를 바탕으로 실제 회수 작업을 생성합니다.
        1) IpReclaimJob 생성
        2) IpReclaimJobItem 생성 (선택된 IP 수만큼)
        3) IpReclaimCandidate 상태를 'IN_PROGRESS'로 변경
        """
        try:
            # 1. 메인 작업 레코드 생성
            new_job = IpReclaimJob(
                session_id="POC_SESSION", # 추후 실제 세션 ID 연동 가능
                requester_id=requester_id,
                main_task_id=main_task_id,
                sub_task_id=sub_task_id,
                job_status="READY"
            )
            self.db.add(new_job)
            self.db.flush() # 생성된 job_id를 얻기 위해 수행

            # 2. 상세 아이템 및 후보군 상태 업데이트
            for ip_data in selected_ips:
                # 상세 아이템 추가
                item = IpReclaimJobItem(
                    ip_reclaim_job_id=new_job.ip_reclaim_job_id,
                    candidate_id=ip_data["candidate_id"],
                    nw_id=ip_data["nw_id"],
                    ip_address=ip_data["ip_address"],
                    owner_team=ip_data["owner_team"],
                    owner_email=ip_data["owner_email"],
                    item_status="READY"
                )
                self.db.add(item)

                # 원본 후보군 상태 변경 (Candidate -> In-Progress)
                candidate = self.db.query(IpReclaimCandidate).get(ip_data["candidate_id"])
                if candidate:
                    candidate.status = "IN_PROGRESS"

            self.db.commit()
            return new_job
            
        except Exception as e:
            self.db.rollback()
            raise e