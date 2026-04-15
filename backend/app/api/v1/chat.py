import json
import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from app.llm.router import master_graph  # 💡 이제 라우터의 그래프를 가져옵니다.
from app.llm.candidate_agent import CandidateAgent
from app.core.database import get_db
from pydantic import BaseModel

router = APIRouter()

class ChatRequest(BaseModel):
    history: List[dict]
    max_per_team: Optional[int] = 4
    selected_ips: Optional[List[dict]] = []

@router.post("/chat")
async def chat(req: ChatRequest):
    # Multi-Agent 환경에 맞는 초기 상태 구성
    initial_state = {
        "messages": req.history, 
        "domain": "",         # 라우터가 판단할 영역 (candidate/reclaim)
        "intent": "",         # 서브 에이전트가 판단할 의도
        "query_plan": {},     # Query Construction 결과
        "selected_ips": req.selected_ips,
        "max_per_team": req.max_per_team
    }
    
    # 이제 특정 에이전트가 아닌 '마스터 그래프'를 실행합니다.
    result = master_graph.invoke(initial_state)
    
    # 마지막 응답 메시지 추출
    last_msg = result["messages"][-1]
    response_content = last_msg.content if hasattr(last_msg, 'content') else last_msg.get('content')
    
    return {
        "content": response_content,
        "max_per_team": result.get("max_per_team", req.max_per_team),
        "selected_ips": result.get("selected_ips", req.selected_ips),
        "domain": result.get("domain") # (디버깅용) 어떤 에이전트가 응답했는지 확인 가능
    }


@router.post("/candidate/upload")
async def upload_candidates_by_context(
    file: UploadFile = File(...),
    history: str = Form(default="[]"),
    usage_threshold: float = Form(default=float(os.getenv("CANDIDATE_USAGE_THRESHOLD", "30"))),
    extraction_batch_id: str = Form(default=""),
    default_owner_email: str = Form(default=os.getenv("CANDIDATE_DEFAULT_OWNER_EMAIL", "no-reply@ipam.local")),
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        raise HTTPException(status_code=400, detail="엑셀 파일(.xlsx/.xlsm)만 업로드할 수 있습니다.")

    try:
        parsed_history = json.loads(history) if history else []
    except Exception:
        parsed_history = []

    content = await file.read()
    agent = CandidateAgent()
    mode = agent.infer_upload_mode_from_history(parsed_history)

    if mode == "finalize":
        batch_id = extraction_batch_id.strip() or f"FINAL-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            result = agent.finalize_candidates_from_excel(
                db=db,
                file_bytes=content,
                extraction_batch_id=batch_id,
                usage_threshold=usage_threshold,
                default_owner_email=default_owner_email,
            )
            message = agent.build_finalize_response_message(result)
            return {**result, "content": message, "mode": "finalize"}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"후보 확정 중 오류가 발생했습니다: {str(e)}")

    batch_id = extraction_batch_id.strip() or f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        result = agent.extract_candidates_from_excel(
            db=db,
            file_bytes=content,
            extraction_batch_id=batch_id,
            usage_threshold=usage_threshold,
            default_owner_email=default_owner_email,
        )
        message = agent.build_extract_response_message(result)
        return {**result, "content": message, "mode": "extract"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"후보 추출 중 오류가 발생했습니다: {str(e)}")