from datetime import date
from typing import Dict, List, Tuple

from sqlalchemy import and_, desc
from sqlalchemy.orm import Session

from app.models.entities import IpReclaimCandidate


class CandidateRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all_candidates_latest(self) -> List[dict]:
        rows = (
            self.db.query(IpReclaimCandidate)
            .order_by(desc(IpReclaimCandidate.created_at))
            .all()
        )
        return [
            {
                "candidate_id": r.candidate_id,
                "nw_id": r.nw_id,
                "ip_address": r.ip_address,
                "owner_team": r.owner_team,
                "status": r.status,
                "extraction_batch_id": r.extraction_batch_id,
                "owner_email": r.owner_email,
            }
            for r in rows
        ]

    def insert_confirmed_candidates(self, selected_ips: List[dict], extraction_batch_id: str = "") -> Dict:
        if not selected_ips:
            return {"inserted_count": 0, "skipped_count": 0}

        inserted = 0
        skipped = 0
        batch_id = extraction_batch_id or f"CONFIRMED-{date.today().strftime('%Y%m%d')}"
        seen_keys: set[Tuple[str, str]] = set()

        for item in selected_ips:
            nw_id = str(item.get("nw_id", "")).strip()
            ip_address = str(item.get("ip_address", "")).strip()
            owner_team = str(item.get("owner_team", "")).strip()
            owner_email = str(item.get("owner_email", "")).strip()

            if not nw_id or not ip_address or not owner_team:
                skipped += 1
                continue

            key = (nw_id, ip_address)
            if key in seen_keys:
                skipped += 1
                continue
            seen_keys.add(key)

            exists = (
                self.db.query(IpReclaimCandidate.candidate_id)
                .filter(and_(IpReclaimCandidate.nw_id == nw_id, IpReclaimCandidate.ip_address == ip_address))
                .first()
            )
            if exists:
                skipped += 1
                continue

            self.db.add(
                IpReclaimCandidate(
                    extraction_batch_id=batch_id,
                    extraction_date=date.today(),
                    nw_id=nw_id,
                    ip_address=ip_address,
                    owner_team=owner_team,
                    owner_email=owner_email,
                    status="READY",
                )
            )
            inserted += 1

        self.db.commit()
        return {"inserted_count": inserted, "skipped_count": skipped}
