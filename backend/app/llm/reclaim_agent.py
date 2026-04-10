import operator
import os
import json
import re
from typing import TypedDict, List, Annotated, Union

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langgraph.graph import StateGraph, END

from app.client.ntoss_client import NtossClient
from app.core.database import SessionLocal
from app.models.entities import IpReclaimCandidate
from app.repositories.reclaim_repository import ReclaimRepository
from app.repositories.job_repository import JobRepository

# 💡 공유 상태 구조 (router.py와 맞추어야 합니다)
class AgentState(TypedDict):
    messages: Annotated[List[Union[dict, BaseMessage]], operator.add]
    intent: str
    query_plan: dict 
    selected_ips: List[dict]
    max_per_team: int

class ReclaimAgent:
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash", 
            temperature=0,
            google_api_key=os.getenv("GOOGLE_API_KEY")
        )
        self.ntoss = NtossClient()

    def _convert_to_messages(self, messages: List[Union[dict, BaseMessage]]) -> List[BaseMessage]:
        converted = []
        for m in messages:
            if isinstance(m, BaseMessage):
                converted.append(m)
                continue
            role = m.get('role', 'user')
            content = m.get('content', '')
            if role == 'user':
                converted.append(HumanMessage(content=content))
            else:
                converted.append(AIMessage(content=content))
        return converted

    def intent_analyzer(self, state: AgentState):
        """[도메인 내부 의도 분석] START, CONFIRM, STATUS, CHAT"""
        history = self._convert_to_messages(state['messages'])
        prompt = "사용자의 의도를 분석하여 START, CONFIRM, STATUS, CHAT 중 하나만 답변하세요."
        response = self.llm.invoke(history + [HumanMessage(content=prompt)])
        raw = response.content.strip().upper()
        
        if "STATUS" in raw: intent = "STATUS"
        elif "CONFIRM" in raw: intent = "CONFIRM"
        elif "START" in raw: intent = "START"
        else: intent = "CHAT"
        return {"intent": intent}

    def query_constructor(self, state: AgentState):
        """[Query Construction] 의도에 따라 검색 필터(JSON) 생성"""
        intent = state["intent"]
        history = self._convert_to_messages(state['messages'])
        
        system_prompt = f"""
        당신은 IPAM 데이터 쿼리 설계자입니다. 사용자의 요청을 분석하여 JSON 쿼리 플랜을 생성하세요.
        대상 의도: {intent}
        1. START인 경우: {{ "team_limit": 4, "total_limit": 20, "target_team": null, "reasoning": "..." }}
        2. STATUS인 경우: {{ "job_id": null, "owner_team": null, "item_status": null, "reasoning": "..." }}
        응답은 오직 JSON만 하세요.
        """
        
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
        try:
            content = re.sub(r'```json|```', '', response.content).strip()
            plan = json.loads(content)
        except:
            plan = {"reasoning": "기본 조회 수행"}
        return {"query_plan": plan}

    def data_fetcher(self, state: AgentState):
        """설계된 plan에 따라 DB 호출"""
        intent = state["intent"]
        plan = state["query_plan"]
        db = SessionLocal()
        try:
            if intent == "START":
                repo = ReclaimRepository(db)
                results = repo.get_flexible_candidates(
                    team_limit=plan.get("team_limit", 4),
                    total_limit=plan.get("total_limit", 20),
                    target_team=plan.get("target_team")
                )
                selected = [{"candidate_id": r.candidate_id, "nw_id": r.nw_id, "ip_address": r.ip_address, "owner_team": r.owner_team, "owner_email": r.owner_email} for r in results]
                return {"selected_ips": selected}

            elif intent == "STATUS":
                repo = JobRepository(db)
                results = repo.get_jobs_by_filter(
                    owner_team=plan.get("owner_team"),
                    item_status=plan.get("item_status")
                )
                fetched_jobs = [{"ip": r.ip_address, "status": r.item_status, "team": r.owner_team} for r in results]
                return {"selected_ips": fetched_jobs}
            return {}
        finally:
            db.close()

    def responder(self, state: AgentState):
        """추출/조회 결과와 query_plan의 reasoning을 결합하여 답변"""
        intent = state["intent"]
        plan = state.get("query_plan", {})
        data = state.get("selected_ips", [])
        
        # 💡 프롬프트를 더 엄격하게 수정
        system_context = f"""
        당신은 LG CNS의 IP 관리 전문가입니다. 
        사용자의 의도: {intent}
        에이전트 판단 근거: {plan.get('reasoning')}
        실제 DB 조회 결과: {data}

        [답변 규칙]
        1. 만약 데이터가 비어있다면([]) "현재 조건에 맞는 진행 중인 작업이 없습니다"라고 명확히 답변하세요. 
        2. "찾아보겠다"거나 "기다려달라"는 말은 절대 하지 마세요. 당신은 이미 조회를 마친 상태입니다.
        3. 데이터가 있다면, 팀별로 요약하거나 상태(Ready, Failed 등)를 구분해서 보기 좋게 보고하세요.
        """
        
        response = self.llm.invoke([SystemMessage(content=system_context)] + state["messages"][-1:])
        return {"messages": [AIMessage(content=response.content)]}

    def task_executor(self, state: AgentState):
        """작업 확정 및 DB Job 생성"""
        ips = state.get("selected_ips", [])
        if not ips:
            return {"messages": [AIMessage(content="확정할 데이터가 없습니다.")]}
        
        db = SessionLocal()
        try:
            repo = JobRepository(db)
            main_res = self.ntoss.create_main_task("ADMIN_DONGHYUK")
            sub_res = self.ntoss.create_sub_task("ADMIN_DONGHYUK", main_res["main_job_id"])
            
            job = repo.create_reclaim_job(
                main_task_id=main_res["main_job_id"],
                sub_task_id=sub_res["sub_job_id"],
                requester_id="ADMIN_DONGHYUK",
                selected_ips=ips
            )
            return {"messages": [AIMessage(content=f"✅ 작업 확정 완료! (ID: {job.main_task_id})")]}
        except Exception as e:
            db.rollback()
            return {"messages": [AIMessage(content=f"오류: {str(e)}")]}
        finally:
            db.close()

# 💡 서브 그래프 빌드 함수
def build_reclaim_graph():
    agent = ReclaimAgent()
    workflow = StateGraph(AgentState)
    
    workflow.add_node("analyzer", agent.intent_analyzer)
    workflow.add_node("constructor", agent.query_constructor)
    workflow.add_node("fetcher", agent.data_fetcher)
    workflow.add_node("responder", agent.responder)
    workflow.add_node("executor", agent.task_executor)

    workflow.set_entry_point("analyzer")
    workflow.add_conditional_edges(
        "analyzer",
        lambda x: x["intent"],
        {
            "START": "constructor", 
            "STATUS": "constructor", 
            "CONFIRM": "executor", 
            "CHAT": "responder"
        }
    )
    workflow.add_edge("constructor", "fetcher")
    workflow.add_edge("fetcher", "responder")
    workflow.add_edge("responder", END)
    workflow.add_edge("executor", END)
    
    return workflow.compile()

# 메인 라우터에서 불러다 쓸 수 있게 그래프 객체 노출
reclaim_graph = build_reclaim_graph()