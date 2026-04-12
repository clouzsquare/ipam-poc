import operator
import os
import json
import re
import logging
from typing import TypedDict, List, Annotated, Union

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langgraph.graph import StateGraph, END

from app.client.ntoss_client import NtossClient
from app.core.database import SessionLocal
from app.models.entities import IpReclaimCandidate, IpReclaimJob, IpReclaimJobItem
from app.repositories.reclaim_job.reclaim_repository import ReclaimRepository
from app.repositories.reclaim_job.job_repository import JobRepository

load_dotenv()
logger = logging.getLogger("RECLAIM_OPERATOR")

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
        """[1] 의도 분석"""
        print("\n🚀 [NODE: intent_analyzer]")
        history = self._convert_to_messages(state['messages'])
        system_prompt = """
        당신은 IPAM AI Assistant의 분류기입니다.
        - REJECT: "제외", "빼줘", "취소", "대상 아님" 등 특정 대상을 작업에서 배제할 때
        - STATUS: "현황", "상태", "진행 상황" 등 현재 작업 데이터를 조회할 때
        - START: 회수 후보군을 새롭게 추출하거나 조회할 때
        - CONFIRM: 준비된 리스트를 확정하고 실제 회수(메일 발송/NTOSS)를 시작할 때
        """
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
        intent = response.content.strip().upper()
        intent = next((i for i in ["START", "CONFIRM", "STATUS", "REJECT"] if i in intent), "CHAT")
        print(f"🎯 분석된 Intent: {intent}")
        return {"intent": intent}

    def query_constructor(self, state: AgentState):
        """[2] 쿼리 설계 - DB 스키마 및 동적 필터 규칙 복구"""
        print(f"🚀 [NODE: query_constructor] (Intent: {state['intent']})")
        intent = state["intent"]
        history = self._convert_to_messages(state['messages'])
        
        db_specs = """
        [DATABASE SPECIFICATION]
        1. Table: ip_reclaim_job (메인 작업) - status: 'READY', 'IN-PROGRESS', 'DONE'
        2. Table: ip_reclaim_job_item (상세 현황) - JOIN: ip_reclaim_job_id
           - item_status: 'READY', 'IN-PROGRESS', 'REJECTED', 'DHCP_REQUESTED', 'DHCP_SUCCESS', 'DHCP_FAILED', 'DEVICE_REQUESTED', 'DEVICE_SUCCESS', 'DEVICE_FAILED'
        
        [QUERY & FILTERING RULES]
        - STATUS/REJECT 시: **무조건** job_status IN ('READY', 'IN-PROGRESS') 필터를 포함하여 활성 작업만 타겟팅합니다.
        - 복합 조건 처리: 사용자가 팀과 IP를 동시에 언급하면 'filters' 리스트에 각각 담으세요.
        - target 종류: owner_team, ip_address(리스트), ip_range, owner_email
        """

        examples = """
        [QUERY EXAMPLES]
        - "클라우드팀 10.100.4.16 제외": { "filters": [ {"target": "owner_team", "value": "클라우드팀"}, {"target": "ip_address", "value": ["10.100.4.16"]} ], "action": "REJECT" }
        - "장애 현황 알려줘": { "filters": [ {"target": "item_status", "value": ["DHCP_FAILED", "DEVICE_FAILED"]} ], "job_status": ["READY", "IN-PROGRESS"], "action": "STATUS" }
        - "현재 작업 상태": { "job_status": ["READY", "IN-PROGRESS"], "filters": [], "action": "STATUS" }
        """

        system_prompt = f"""
        당신은 IPAM 쿼리 설계자입니다. 사용자의 "{intent}" 요청에 대해 DB를 정확히 조회할 수 있는 JSON 플랜을 생성하세요.
        {db_specs}
        {examples}
        결과는 마크다운 없이 순수 JSON만 출력하세요.
        """
        
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
        try:
            content = re.sub(r'```json|```', '', response.content).strip()
            plan = json.loads(content)
        except:
            plan = {"filters": [], "job_status": ["READY", "IN-PROGRESS"]}
            
        print(f"📋 생성된 Plan: {plan}")
        return {"query_plan": plan}

    def data_fetcher(self, state: AgentState):
        """[3] 데이터 조회"""
        print("🚀 [NODE: data_fetcher]")
        intent = state["intent"]
        plan = state.get("query_plan", {})
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

            if intent == "STATUS":
                repo = JobRepository(db)
                results = repo.get_jobs_by_filter(
                    owner_team=plan.get("owner_team"),
                    job_status=["READY", "IN-PROGRESS"]
                )
                return {"selected_ips": [{"ip": r.ip_address, "status": r.item_status, "team": r.owner_team} for r in results]}
            return {"selected_ips": []}
        finally:
            db.close()

    def reject_handler(self, state: AgentState):
        """[4] 제외 처리 - 동적 필터 기반 일괄 업데이트 및 즉시 응답"""
        print("🚀 [NODE: reject_handler]")
        plan = state.get("query_plan", {})
        filters = plan.get("filters", [])
        db = SessionLocal()
        try:
            repo = JobRepository(db)
            # Repository의 동적 업데이트 메서드 호출
            count = repo.bulk_update_item_status_by_filters(filters, "REJECTED")
            
            # 기준 정보 요약 (응답용)
            criteria_msg = ", ".join([f"{f['target']}: {f['value']}" for f in filters])
            msg = f"🚫 IPAM AI Assistant입니다. 요청하신 조건({criteria_msg})에 해당하는 대상 총 {count}건을 이번 회수 작업에서 제외 처리하였습니다."
            return {"messages": [AIMessage(content=msg)]}
        finally:
            db.close()

    def responder(self, state: AgentState):
        """[5] 결과 보고 - 'IPAM AI Assistant' 페르소나 및 상태 정의 준수"""
        print("🚀 [NODE: responder]")
        data = state.get("selected_ips", [])
        
        if not data:
            return {"messages": [AIMessage(content="IPAM AI Assistant입니다. 현재 진행 중인 활성 작업이 없습니다.")]}

        system_context = f"""
        당신은 'IPAM AI Assistant'입니다. 제공된 데이터({data})를 기반으로 현황을 보고하세요.
        
        [상태 정의]
        - READY: 회수 대기 중 (작업 시작 전)
        - IN-PROGRESS: 담당자 확인 중 (메일 발송 완료)
        - DHCP_SUCCESS / DEVICE_SUCCESS: 단계별 회수 완료
        - FAILED: 회수 장애 발생 (IP 명시 필수)

        [보고 규칙]
        - 인사는 "IPAM AI Assistant입니다."로 시작.
        - 수치 요약 후 개조식으로 상세 보고.
        """
        response = self.llm.invoke([SystemMessage(content=system_context)] + state["messages"][-1:])
        return {"messages": [AIMessage(content=response.content)]}

    def task_executor(self, state: AgentState):
        """[6] 작업 확정"""
        print("🚀 [NODE: task_executor]")
        ips = state.get("selected_ips", [])
        if not ips: return {"messages": [AIMessage(content="확정할 대상 데이터가 없습니다.")]}
        db = SessionLocal()
        try:
            repo = JobRepository(db)
            main_res = self.ntoss.create_main_task("ADMIN_DONGHYUK")
            sub_res = self.ntoss.create_sub_task("ADMIN_DONGHYUK", main_res["main_job_id"])
            self.ntoss.register_targets(sub_res["sub_job_id"], ips)
            job = repo.create_reclaim_job(main_res["main_job_id"], sub_res["sub_job_id"], "ADMIN_DONGHYUK", ips)
            db.commit()
            return {"messages": [AIMessage(content=f"✅ 작업 확정 완료 (ID: {job.main_task_id})")]}
        except Exception as e:
            db.rollback()
            return {"messages": [AIMessage(content=f"❌ 오류 발생: {str(e)}")]}
        finally: db.close()

def build_reclaim_graph():
    agent = ReclaimAgent()
    workflow = StateGraph(AgentState)
    workflow.add_node("analyzer", agent.intent_analyzer)
    workflow.add_node("constructor", agent.query_constructor)
    workflow.add_node("fetcher", agent.data_fetcher)
    workflow.add_node("responder", agent.responder)
    workflow.add_node("executor", agent.task_executor)
    workflow.add_node("rejecter", agent.reject_handler)

    workflow.set_entry_point("analyzer")
    workflow.add_conditional_edges("analyzer", lambda x: x["intent"], {
        "START": "constructor", "STATUS": "constructor", "REJECT": "constructor", "CONFIRM": "executor", "CHAT": "responder"
    })
    workflow.add_conditional_edges("constructor", lambda x: x["intent"], {
        "START": "fetcher", "STATUS": "fetcher", "REJECT": "rejecter"
    })
    
    workflow.add_edge("fetcher", "responder")
    workflow.add_edge("responder", END)
    workflow.add_edge("rejecter", END)
    workflow.add_edge("executor", END)
    
    return workflow.compile()

reclaim_graph = build_reclaim_graph()