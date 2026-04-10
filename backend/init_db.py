from app.core.database import Base, engine, SessionLocal
from app.models.entities import IpReclaimCandidate
import random
from datetime import date

def init_db():
    # 테이블이 이미 있다면 삭제하고 새로 생성 (초기화용)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    teams = ["인프라팀", "플랫폼팀", "보안팀", "데이터팀", "클라우드팀", "솔루션팀"]
    managers = ["김철수", "이영희", "박지민", "최동혁", "정미경", "한상우"]
    emails = ["chulsu@lgnw.com", "younghee@lgnw.com", "jimin@lgnw.com", "donghyuk@lgnw.com", "mikyoung@lgnw.com", "sangwoo@lgnw.com"]
    
    candidates = []
    for i in range(1, 51):
        idx = i % len(teams)
        # IpReclaimCandidate 엔티티 정의에 맞게 필드명 수정
        candidates.append(IpReclaimCandidate(
            extraction_batch_id=f"BATCH-2026-04-{i//10 + 1}", # 필수값 추가
            extraction_date=date.today(),                      # 필수값 추가
            nw_id=f"NW{str(i).zfill(3)}",
            ip_address=f"10.100.{idx}.{i}",                    # ip -> ip_address
            owner_team=teams[idx],                             # team -> owner_team
            owner_email=emails[idx],                           # manager -> owner_email
            status="READY"                                     # status (ReclaimStatus.READY 대신 문자열로 우선 테스트)
        ))
    
    try:
        db.add_all(candidates)
        db.commit()
        print("✅ Database initialized with 50 mock candidates.")
    except Exception as e:
        db.rollback()
        print(f"❌ Error during initialization: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    init_db()