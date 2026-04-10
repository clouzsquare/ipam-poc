from sqlalchemy.orm import Session
from sqlalchemy import func, select, and_, delete
from app.models.entities import IpReclaimCandidate, IpReclaimPreview
from app.models.enums import ReclaimStatus

class ReclaimRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_flexible_candidates(self, team_limit: int = 4, total_limit: int = 20, target_team: str = None):
        """
        ORM 기반 팀별 균형 추출 로직 (MySQL 8.0 에러 해결 버전)
        """
        # 1. 윈도우 함수 정의
        # ERROR 3587 해결을 위해 ORDER BY에 rand() 대신 candidate_id를 사용합니다.
        window_func = func.row_number().over(
            partition_by=IpReclaimCandidate.owner_team,
            order_by=IpReclaimCandidate.candidate_id  # 결정적인(Deterministic) 컬럼으로 변경
        ).label("team_rn")

        # 2. 필터 조건 구성
        # 팁: Enum 객체(ReclaimStatus.READY) 대신 문자열 "READY"를 직접 비교하는 것이 
        # DB 드라이버에 따라 더 안전할 수 있습니다.
        filters = [
            IpReclaimCandidate.status == "READY",
            IpReclaimCandidate.extraction_date == func.curdate()
        ]
        
        if target_team:
            filters.append(IpReclaimCandidate.owner_team == target_team)

        # 3. 서브쿼리
        subq = (
            select(IpReclaimCandidate, window_func)
            .where(and_(*filters))
            .subquery()
        )

        # 4. 메인 쿼리
        # 팀별로는 ID 순으로 4개를 뽑더라도, 전체 20개를 섞는 단계에서 
        # 다시 RAND()가 적용되므로 최종 결과의 랜덤성은 유지됩니다.
        stmt = (
            select(subq)
            .where(subq.c.team_rn <= team_limit)
            .order_by(func.rand()) 
            .limit(total_limit)
        )

        return self.db.execute(stmt).fetchall()