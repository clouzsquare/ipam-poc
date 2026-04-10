from typing import List, Optional
from fastapi import APIRouter
from app.llm.router import master_graph  # 💡 이제 라우터의 그래프를 가져옵니다.
from pydantic import BaseModel

router = APIRouter()

class ChatRequest(BaseModel):
    history: List[dict]
    max_per_team: Optional[int] = 4
    selected_ips: Optional[List[dict]] = []

@router.post("/chat")
async def chat(req: ChatRequest):
    # 💡 Multi-Agent 환경에 맞는 초기 상태 구성
    initial_state = {
        "messages": req.history, 
        "domain": "",         # 라우터가 판단할 영역 (candidate/reclaim)
        "intent": "",         # 서브 에이전트가 판단할 의도
        "query_plan": {},     # Query Construction 결과
        "selected_ips": req.selected_ips,
        "max_per_team": req.max_per_team
    }
    
    # 💡 이제 특정 에이전트가 아닌 '마스터 그래프'를 실행합니다.
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