from sqlalchemy.orm import Session
from sqlalchemy import desc, and_, select
from typing import Union, List
from app.models.entities import IpReclaimJob, IpReclaimJobItem, IpReclaimCandidate

class JobRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_jobs_by_filter(self, 
                           job_id: str = None, 
                           owner_team: str = None, 
                           item_status: Union[str, List[str]] = None, # 💡 타입 힌트 추가
                           job_status: Union[str, List[str]] = None, 
                           limit: int = 50):
        
        query = self.db.query(IpReclaimJobItem).join(
            IpReclaimJob, IpReclaimJobItem.ip_reclaim_job_id == IpReclaimJob.ip_reclaim_job_id
        )
        
        filters = []
        if job_id:
            filters.append(IpReclaimJob.main_task_id == job_id)
        if owner_team:
            filters.append(IpReclaimJobItem.owner_team == owner_team)
            
        if item_status:
            if isinstance(item_status, list):
                filters.append(IpReclaimJobItem.item_status.in_(item_status))
            else:
                filters.append(IpReclaimJobItem.item_status == item_status)
            
        if job_status:
            if isinstance(job_status, list):
                filters.append(IpReclaimJob.job_status.in_(job_status))
            else:
                filters.append(IpReclaimJob.job_status == job_status)
            
        if filters:
            query = query.filter(and_(*filters))
            
        return query.order_by(desc(IpReclaimJobItem.created_at)).limit(limit).all()

    def get_latest_job_summary(self):
        latest_job = self.db.query(IpReclaimJob).order_by(desc(IpReclaimJob.ip_reclaim_job_id)).first()
        if not latest_job: return None
        items = self.db.query(IpReclaimJobItem).filter(
            IpReclaimJobItem.ip_reclaim_job_id == latest_job.ip_reclaim_job_id
        ).all()
        return {"job_info": latest_job, "items": items}

    def create_reclaim_job(self, main_task_id: str, sub_task_id: str, requester_id: str, selected_ips: list, initial_item_status: str = "IN-PROGRESS"):
        try:
            new_job = IpReclaimJob(
                session_id="POC_SESSION",
                requester_id=requester_id,
                main_task_id=main_task_id,
                sub_task_id=sub_task_id,
                job_status="READY"
            )
            self.db.add(new_job)
            self.db.flush()

            for ip_data in selected_ips:
                item = IpReclaimJobItem(
                    ip_reclaim_job_id=new_job.ip_reclaim_job_id,
                    candidate_id=ip_data["candidate_id"],
                    nw_id=ip_data["nw_id"],
                    ip_address=ip_data["ip_address"],
                    owner_team=ip_data["owner_team"],
                    owner_email=ip_data["owner_email"],
                    item_status=initial_item_status
                )
                self.db.add(item)
                candidate = self.db.query(IpReclaimCandidate).get(ip_data["candidate_id"])
                if candidate: candidate.status = "IN_PROGRESS"

            self.db.commit()
            return new_job
        except Exception as e:
            self.db.rollback()
            raise e
    
    def update_item_status(self, job_id: int, ip_address: str, new_status: str):
        item = self.db.query(IpReclaimJobItem).filter(
            and_(IpReclaimJobItem.ip_reclaim_job_id == job_id, IpReclaimJobItem.ip_address == ip_address)
        ).first()
        if item:
            item.item_status = new_status
            self.db.commit()
            return True
        return False
    
    def get_active_job(self):
        """현재 활성 작업(READY, IN-PROGRESS) 조회 - 가장 최근 작업 반환"""
        return (
            self.db.query(IpReclaimJob)
            .filter(IpReclaimJob.job_status.in_(["READY", "IN-PROGRESS"]))
            .order_by(desc(IpReclaimJob.ip_reclaim_job_id))
            .first()
        )

    def get_items_by_job_and_status(self, job_id: int, statuses: List[str]):
        """특정 잡의 특정 상태 아이템 목록 조회"""
        return (
            self.db.query(IpReclaimJobItem)
            .filter(
                and_(
                    IpReclaimJobItem.ip_reclaim_job_id == job_id,
                    IpReclaimJobItem.item_status.in_(statuses)
                )
            )
            .all()
        )

    def update_job_status(self, job_id: int, new_status: str):
        """잡 상태 업데이트"""
        job = self.db.query(IpReclaimJob).filter(
            IpReclaimJob.ip_reclaim_job_id == job_id
        ).first()
        if job:
            job.job_status = new_status
            self.db.commit()
        return job

    def update_item_status_by_id(self, item_id: int, new_status: str, **extra_fields):
        """아이템 ID 기반 상태 + 추가 필드 업데이트"""
        item = self.db.query(IpReclaimJobItem).filter(
            IpReclaimJobItem.ip_reclaim_job_item_id == item_id
        ).first()
        if item:
            item.item_status = new_status
            for key, val in extra_fields.items():
                if hasattr(item, key):
                    setattr(item, key, val)
            self.db.commit()
        return item

    def bulk_update_item_status_by_filters(self, filter_list: List[dict], new_status: str):
        """
        [안전장치 강화] 필터가 명확하지 않으면 업데이트를 수행하지 않습니다.
        """
        try:
            # 필터 리스트 자체가 비어있으면 즉시 리턴
            if not filter_list:
                print("⚠️ [DEBUG] 필터 리스트가 비어있어 업데이트를 중단합니다.")
                return 0

            # 1. 대상 ID 추출을 위한 기본 쿼리
            id_query = select(IpReclaimJobItem.ip_reclaim_job_item_id).join(IpReclaimJob)
            id_query = id_query.where(IpReclaimJob.job_status.in_(["READY", "IN-PROGRESS"]))

            # 2. 동적 필터 적용 여부 체크를 위한 카운트
            applied_filter_count = 0

            for f in filter_list:
                target = f.get("target")
                val = f.get("value")
                if not val: continue # 값이 없으면 건너뜀

                if target == "owner_team":
                    id_query = id_query.where(IpReclaimJobItem.owner_team == val)
                    applied_filter_count += 1
                elif target == "ip_address":
                    if isinstance(val, list): id_query = id_query.where(IpReclaimJobItem.ip_address.in_(val))
                    else: id_query = id_query.where(IpReclaimJobItem.ip_address == val)
                    applied_filter_count += 1
                elif target == "ip_range":
                    id_query = id_query.where(IpReclaimJobItem.ip_address.like(f"{val}%"))
                    applied_filter_count += 1
                elif target == "owner_email":
                    id_query = id_query.where(IpReclaimJobItem.owner_email.contains(val))
                    applied_filter_count += 1

            # 💡 [안전장치] 유효한 필터가 하나도 적용되지 않았다면 업데이트 중단 (전체 업데이트 방지)
            if applied_filter_count == 0:
                print("⚠️ [DEBUG] 유효한 필터 조건이 없어 업데이트를 수행하지 않습니다.")
                return 0

            # 3. ID 추출 및 업데이트 (MySQL 1093 에러 회피)
            target_ids = self.db.execute(id_query).scalars().all()
            if not target_ids: return 0

            updated_count = (
                self.db.query(IpReclaimJobItem)
                .filter(IpReclaimJobItem.ip_reclaim_job_item_id.in_(target_ids))
                .update({IpReclaimJobItem.item_status: new_status}, synchronize_session=False)
            )
            self.db.commit()
            return updated_count
        except Exception as e:
            self.db.rollback()
            raise e