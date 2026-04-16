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
from app.repositories.reclaim_job.reclaim_repository import ReclaimRepository
from app.repositories.reclaim_job.job_repository import JobRepository
from app.utils.gmail_service import send_reclaim_notification

load_dotenv()
logger = logging.getLogger("RECLAIM_OPERATOR")


class AgentState(TypedDict):
    messages: Annotated[List[Union[dict, BaseMessage]], operator.add]
    intents: List[str]       # 처리 대기 중인 인텐트 큐 (예: ["REJECT", "START", "STATUS"])
    current_intent: str      # 현재 처리 중인 인텐트
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
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "user":
                converted.append(HumanMessage(content=content))
            else:
                converted.append(AIMessage(content=content))
        return converted

    # ───────────────────────────────────────────────
    # [NODE 1] intent_analyzer
    # ───────────────────────────────────────────────
    def intent_analyzer(self, state: AgentState):
        """사용자 요청에서 처리 순서대로 다중 인텐트 추출"""
        print("\n🚀 [NODE: intent_analyzer]")
        history = self._convert_to_messages(state["messages"])

        system_prompt = """
당신은 IPAM AI Assistant의 의도 분류기입니다.
사용자 요청에서 수행해야 할 작업을 실행 순서대로 추출하세요.

[의도 종류]
- REJECT  : "제외", "빼줘", "취소", "대상 아님" 등 특정 대상을 작업에서 배제
- START   : 금일 IP 회수 후보군 추출/조회 요청
- CONFIRM : 준비된 목록을 확정하고 메일 발송 및 NTOSS 등록
- STATUS  : "현황", "상태", "진행 상황" 등 현재 작업 데이터 조회
- CHAT    : 단순 인사, 사용법 문의 등

[규칙]
- 여러 의도가 있으면 실행 순서대로 콤마로 구분 (예: REJECT,START,STATUS)
- 단일 의도면 하나만 출력
- 반드시 위 목록에서만 선택
- 순수하게 콤마 구분 의도만 출력 (다른 텍스트 없이)

예시)
- "1.1.1.1 빼고 오늘 작업 시작해줘. 현황도 알려줘" → REJECT,START,STATUS
- "오늘 IP 회수 시작해줘" → START
- "확정해줘" → CONFIRM
- "진행 현황 알려줘" → STATUS
"""
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
        raw = response.content.strip().upper()

        valid = {"START", "CONFIRM", "STATUS", "REJECT", "CHAT"}
        intents = [i.strip() for i in raw.split(",") if i.strip() in valid]
        if not intents:
            intents = ["CHAT"]

        print(f"🎯 분석된 Intents: {intents}")
        return {"intents": intents}

    # ───────────────────────────────────────────────
    # [NODE 2] dispatcher
    # ───────────────────────────────────────────────
    def dispatcher(self, state: AgentState):
        """인텐트 큐에서 다음 인텐트를 꺼내 current_intent 설정"""
        intents = list(state.get("intents", []))
        if not intents:
            print("🔀 [DISPATCHER] 모든 인텐트 처리 완료 → DONE")
            return {"current_intent": "DONE"}
        current = intents.pop(0)
        print(f"🔀 [DISPATCHER] 처리: {current} | 남은 큐: {intents}")
        return {"current_intent": current, "intents": intents}

    # ───────────────────────────────────────────────
    # [NODE 3] query_constructor
    # ───────────────────────────────────────────────
    def query_constructor(self, state: AgentState):
        """DB 조회 플랜 생성 (동적 필터 + limit 파라미터 추출)"""
        intent = state["current_intent"]
        print(f"🚀 [NODE: query_constructor] (Intent: {intent})")
        history = self._convert_to_messages(state["messages"])
        max_per_team = state.get("max_per_team", 4)

        db_specs = """
[DATABASE SPECIFICATION]
1. ip_reclaim_job (메인 작업): job_status = 'READY' | 'IN-PROGRESS' | 'DONE'
2. ip_reclaim_job_item (상세): item_status = 'READY' | 'IN-PROGRESS' | 'REJECTED' |
   'DHCP_SUCCESS' | 'DHCP_FAILED' | 'DEVICE_SUCCESS' | 'DEVICE_FAILED'

[FILTERING RULES]
- STATUS/REJECT: job_status IN ('READY', 'IN-PROGRESS') 필터 필수
- REJECT target 종류: owner_team, ip_address(리스트), ip_range, owner_email
- START: team_limit(팀당 최대), total_limit(전체 최대) 포함
"""
        examples = f"""
[EXAMPLES]
START 기본: {{"team_limit": {max_per_team}, "total_limit": 20, "action": "START"}}
START 변경: {{"team_limit": 3, "total_limit": 15, "action": "START"}}  ← "팀당 3개, 15개로 시작"
REJECT: {{"filters": [{{"target": "owner_team", "value": "클라우드팀"}}, {{"target": "ip_address", "value": ["10.0.0.1"]}}], "action": "REJECT"}}
STATUS: {{"job_status": ["READY", "IN-PROGRESS"], "filters": [], "action": "STATUS"}}
STATUS 장애: {{"filters": [{{"target": "item_status", "value": ["DHCP_FAILED", "DEVICE_FAILED"]}}], "job_status": ["READY", "IN-PROGRESS"], "action": "STATUS"}}
"""
        system_prompt = f"""
당신은 IPAM 쿼리 설계자입니다. 사용자의 "{intent}" 요청에 맞는 JSON 쿼리 플랜을 생성하세요.
{db_specs}
{examples}
마크다운 없이 순수 JSON만 출력하세요.
"""
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
        try:
            content = re.sub(r"```json|```", "", response.content).strip()
            plan = json.loads(content)
        except Exception:
            plan = {"filters": [], "job_status": ["READY", "IN-PROGRESS"]}

        # START 인텐트에서 사용자가 팀당 제한을 명시한 경우 반영
        if intent == "START":
            new_max = plan.get("team_limit", max_per_team)
            print(f"📋 Plan: {plan} | max_per_team: {new_max}")
            return {"query_plan": plan, "max_per_team": new_max}

        print(f"📋 Plan: {plan}")
        return {"query_plan": plan}

    # ───────────────────────────────────────────────
    # [NODE 4] data_fetcher
    # ───────────────────────────────────────────────
    def data_fetcher(self, state: AgentState):
        """DB에서 데이터 조회 (START: 후보군 추출, STATUS: 작업 현황)"""
        print("🚀 [NODE: data_fetcher]")
        intent = state["current_intent"]
        plan = state.get("query_plan", {})
        max_per_team = state.get("max_per_team", 4)
        db = SessionLocal()
        try:
            if intent == "START":
                repo = ReclaimRepository(db)
                results = repo.get_flexible_candidates(
                    team_limit=plan.get("team_limit", max_per_team),
                    total_limit=plan.get("total_limit", 20)
                )
                selected = [
                    {
                        "candidate_id": r.candidate_id,
                        "nw_id": r.nw_id,
                        "ip_address": r.ip_address,
                        "owner_team": r.owner_team,
                        "owner_email": r.owner_email,
                    }
                    for r in results
                ]
                print(f"📦 후보 추출: {len(selected)}건")
                return {"selected_ips": selected}

            if intent == "STATUS":
                repo = JobRepository(db)
                # 필터에서 item_status 조건 추출
                item_status_filter = None
                for f in plan.get("filters", []):
                    if f.get("target") == "item_status":
                        item_status_filter = f.get("value")
                        break

                results = repo.get_jobs_by_filter(
                    item_status=item_status_filter,
                    job_status=["READY", "IN-PROGRESS"]
                )
                status_data = [
                    {"ip": r.ip_address, "status": r.item_status, "team": r.owner_team}
                    for r in results
                ]
                print(f"📦 상태 조회: {len(status_data)}건")
                return {"selected_ips": status_data}

            return {"selected_ips": []}
        finally:
            db.close()

    # ───────────────────────────────────────────────
    # [NODE 5] reject_handler
    # ───────────────────────────────────────────────
    def reject_handler(self, state: AgentState):
        """특정 조건의 아이템을 REJECTED 상태로 일괄 업데이트"""
        print("🚀 [NODE: reject_handler]")
        plan = state.get("query_plan", {})
        filters = plan.get("filters", [])
        db = SessionLocal()
        try:
            repo = JobRepository(db)
            count = repo.bulk_update_item_status_by_filters(filters, "REJECTED")
            criteria = ", ".join([f"{f['target']}: {f['value']}" for f in filters])
            msg = (
                f"IPAM AI Assistant입니다. "
                f"요청하신 조건({criteria})에 해당하는 대상 {count}건을 "
                f"이번 회수 작업에서 제외 처리하였습니다."
            )
            return {"messages": [AIMessage(content=msg)]}
        finally:
            db.close()

    # ───────────────────────────────────────────────
    # [NODE 6] task_executor (CONFIRM)
    # ───────────────────────────────────────────────
    def task_executor(self, state: AgentState):
        """
        작업 확정:
        1. NTOSS 메인/서브 작업 생성
        2. DB 작업 등록 (job + items, status=IN-PROGRESS)
        3. 담당자별 안내 메일 발송
        """
        print("🚀 [NODE: task_executor]")
        ips = state.get("selected_ips", [])
        if not ips:
            msg = (
                "IPAM AI Assistant입니다. 확정할 대상 데이터가 없습니다. "
                "먼저 금일 작업 목록을 조회한 후 확정해 주세요."
            )
            return {"messages": [AIMessage(content=msg)]}

        db = SessionLocal()
        try:
            repo = JobRepository(db)

            # 1. NTOSS 작업 생성
            main_res = self.ntoss.create_main_task("ADMIN_DONGHYUK")
            sub_res = self.ntoss.create_sub_task("ADMIN_DONGHYUK", main_res["main_job_id"])
            self.ntoss.register_targets(sub_res["sub_job_id"], ips)

            # 2. DB 작업 등록 (아이템 상태: IN-PROGRESS)
            job = repo.create_reclaim_job(
                main_task_id=main_res["main_job_id"],
                sub_task_id=sub_res["sub_job_id"],
                requester_id="ADMIN_DONGHYUK",
                selected_ips=ips,
                initial_item_status="IN-PROGRESS",
            )

            # 3. 담당자 안내 메일 발송
            mail_success = 0
            mail_failed = []
            for ip_data in ips:
                email = ip_data.get("owner_email", "")
                if not email:
                    continue
                ok = send_reclaim_notification(
                    owner_email=email,
                    ip_address=ip_data["ip_address"],
                    nw_id=ip_data.get("nw_id", ""),
                    owner_team=ip_data.get("owner_team", ""),
                )
                if ok:
                    mail_success += 1
                else:
                    mail_failed.append(ip_data["ip_address"])

            lines = [
                "IPAM AI Assistant입니다. 금일 IP 회수 작업이 확정되었습니다.",
                "",
                "**NTOSS 작업 정보**",
                f"- 메인 작업 ID: {main_res['main_job_id']}",
                f"- 서브 작업 ID: {sub_res['sub_job_id']}",
                f"- 등록 대상: {len(ips)}건",
                "",
                "**담당자 안내 메일**",
                f"- 발송 완료: {mail_success}건",
            ]
            if mail_failed:
                lines.append(f"- 발송 실패: {', '.join(mail_failed)}")
            lines += [
                "",
                "담당자 메일 회신 후 11:00 DHCP 회수, 17:00 장비 회수가 진행됩니다.",
                "(/scheduler/mail-reply 로 회신 결과를 처리할 수 있습니다.)",
            ]

            return {"messages": [AIMessage(content="\n".join(lines))]}

        except Exception as e:
            db.rollback()
            logger.error(f"task_executor error: {e}")
            return {"messages": [AIMessage(content=f"오류가 발생했습니다: {str(e)}")]}
        finally:
            db.close()

    # ───────────────────────────────────────────────
    # [NODE 7] responder
    # ───────────────────────────────────────────────
    def responder(self, state: AgentState):
        """조회 데이터를 IPAM AI Assistant 페르소나로 포맷팅하여 응답"""
        print("🚀 [NODE: responder]")
        intent = state.get("current_intent", "")
        data = state.get("selected_ips", [])

        if not data:
            if intent == "START":
                msg = "IPAM AI Assistant입니다. 현재 회수 가능한 후보가 없습니다. (READY 상태 후보 없음)"
            elif intent == "STATUS":
                msg = "IPAM AI Assistant입니다. 현재 진행 중인 활성 작업이 없습니다."
            else:
                msg = "IPAM AI Assistant입니다. 요청하신 내용에 대한 데이터가 없습니다."
            return {"messages": [AIMessage(content=msg)]}

        system_context = f"""
당신은 'IPAM AI Assistant'입니다. 제공된 데이터를 기반으로 현황을 보고하세요.

[현재 인텐트: {intent}]
[데이터 ({len(data)}건): {json.dumps(data, ensure_ascii=False)}]

[상태 정의]
- READY: 회수 대기 중
- IN-PROGRESS: 담당자 확인 중 (메일 발송 완료)
- DHCP_SUCCESS / DEVICE_SUCCESS: 단계별 회수 완료
- DHCP_FAILED / DEVICE_FAILED: 장애 발생

[보고 규칙]
- "IPAM AI Assistant입니다."로 시작
- START: 금일 회수 예정 목록을 팀별로 CSV 형식(IP, NW ID, 팀, 이메일)으로 정리
- STATUS: 상태별 건수 요약 후 상세 목록 제공
- 수치 요약 후 개조식으로 상세 보고
"""
        response = self.llm.invoke([SystemMessage(content=system_context), HumanMessage(content="위 데이터를 바탕으로 보고해주세요.")])
        return {"messages": [AIMessage(content=response.content)]}

    # ───────────────────────────────────────────────
    # [NODE 8] chat_responder (CHAT / fallback)
    # ───────────────────────────────────────────────
    def chat_responder(self, state: AgentState):
        """일반 대화 응답"""
        print("🚀 [NODE: chat_responder]")
        history = self._convert_to_messages(state["messages"])
        system_prompt = """
당신은 'IPAM AI Assistant'입니다.
IPAM 시스템의 일일 IP 회수 업무를 지원합니다.

주요 기능:
- 금일 IP 회수 후보 추출 (예: "오늘 작업 시작해줘")
- 특정 IP/팀 제외 처리 (예: "클라우드팀 빼줘")
- 작업 확정 및 담당자 메일 발송 (예: "확정해줘")
- 진행 현황 조회 (예: "현황 알려줘")

자연스럽게 도움을 제공하세요. "IPAM AI Assistant입니다."로 시작하세요.
"""
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
        return {"messages": [AIMessage(content=response.content)]}


# ───────────────────────────────────────────────────────────
# Graph 빌드
# ───────────────────────────────────────────────────────────
def build_reclaim_graph():
    agent = ReclaimAgent()
    workflow = StateGraph(AgentState)

    # 노드 등록
    workflow.add_node("analyzer", agent.intent_analyzer)
    workflow.add_node("dispatcher", agent.dispatcher)
    workflow.add_node("constructor", agent.query_constructor)
    workflow.add_node("fetcher", agent.data_fetcher)
    workflow.add_node("responder", agent.responder)
    workflow.add_node("chat_responder", agent.chat_responder)
    workflow.add_node("rejecter", agent.reject_handler)
    workflow.add_node("executor", agent.task_executor)

    # 진입점: analyzer → dispatcher
    workflow.set_entry_point("analyzer")
    workflow.add_edge("analyzer", "dispatcher")

    # dispatcher: current_intent에 따라 분기
    workflow.add_conditional_edges(
        "dispatcher",
        lambda x: x["current_intent"],
        {
            "REJECT":  "constructor",
            "START":   "constructor",
            "STATUS":  "constructor",
            "CONFIRM": "executor",
            "CHAT":    "chat_responder",
            "DONE":    END,
        },
    )

    # constructor: current_intent에 따라 분기
    workflow.add_conditional_edges(
        "constructor",
        lambda x: x["current_intent"],
        {
            "REJECT": "rejecter",
            "START":  "fetcher",
            "STATUS": "fetcher",
        },
    )

    # 각 핸들러 완료 후 dispatcher로 복귀 (다음 인텐트 처리)
    workflow.add_edge("fetcher",       "responder")
    workflow.add_edge("responder",     "dispatcher")
    workflow.add_edge("rejecter",      "dispatcher")
    workflow.add_edge("executor",      "dispatcher")
    workflow.add_edge("chat_responder", END)

    return workflow.compile()


reclaim_graph = build_reclaim_graph()
