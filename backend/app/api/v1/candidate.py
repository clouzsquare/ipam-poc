import os
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.llm.candidate_agent import CandidateAgent

router = APIRouter()


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


@router.post("/candidate/extract")
async def extract_candidates_from_excel(
    file: UploadFile = File(...),
    usage_threshold: float = Form(default=float(os.getenv("CANDIDATE_USAGE_THRESHOLD", "30"))),
    extraction_batch_id: str = Form(default=""),
    default_owner_email: str = Form(default=os.getenv("CANDIDATE_DEFAULT_OWNER_EMAIL", "no-reply@ipam.local")),
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        raise HTTPException(status_code=400, detail="엑셀 파일(.xlsx/.xlsm)만 업로드할 수 있습니다.")

    batch_id = extraction_batch_id.strip() or f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    content = await file.read()

    try:
        agent = CandidateAgent()
        result = agent.extract_candidates_from_excel(
            db=db,
            file_bytes=content,
            extraction_batch_id=batch_id,
            usage_threshold=usage_threshold,
            default_owner_email=default_owner_email,
        )
        message = agent.build_extract_response_message(result)
        return {**result, "content": message}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"후보 추출 중 오류가 발생했습니다: {str(e)}")


@router.post("/candidate/finalize")
async def finalize_candidates_from_excel(
    file: UploadFile = File(...),
    usage_threshold: float = Form(default=float(os.getenv("CANDIDATE_USAGE_THRESHOLD", "30"))),
    extraction_batch_id: str = Form(default=""),
    default_owner_email: str = Form(default=os.getenv("CANDIDATE_DEFAULT_OWNER_EMAIL", "no-reply@ipam.local")),
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        raise HTTPException(status_code=400, detail="엑셀 파일(.xlsx/.xlsm)만 업로드할 수 있습니다.")

    batch_id = extraction_batch_id.strip() or f"FINAL-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    content = await file.read()

    try:
        agent = CandidateAgent()
        result = agent.finalize_candidates_from_excel(
            db=db,
            file_bytes=content,
            extraction_batch_id=batch_id,
            usage_threshold=usage_threshold,
            default_owner_email=default_owner_email,
        )
        message = agent.build_finalize_response_message(result)
        return {**result, "content": message}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"후보 확정 중 오류가 발생했습니다: {str(e)}")

