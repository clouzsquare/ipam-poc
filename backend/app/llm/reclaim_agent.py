import operator
import os
import json
import re
import logging
from typing import TypedDict, List, Annotated, Union

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage
from langgraph.graph import StateGraph, END
from app.client.ntoss_client import NtossClient
from app.llm.provider import get_provider
from app.llm.prompts import (
    RECLAIM_CHAT_RESPONDER,
    RECLAIM_INTENT_ANALYZER,
    build_query_constructor_prompt,
    build_reclaim_responder_prompt,
)
from app.core.database import SessionLocal
from app.repositories.reclaim_job.reclaim_repository import ReclaimRepository
from app.repositories.reclaim_job.job_repository import JobRepository
from app.utils.gmail_service import send_reclaim_notification

load_dotenv()
logger = logging.getLogger("RECLAIM_OPERATOR")


class AgentState(TypedDict):
    messages: Annotated[List[Union[dict, BaseMessage]], operator.add]
    intents: List[str]        # 처리 대기 중인 인텐트 큐 (예: ["REJECT", "START", "STATUS"])
    current_intent: str       # 현재 처리 중인 인텐트
    query_plan: dict
    selected_ips: List[dict]
    max_per_team: int
    excluded_filters: List[dict]  # 확정 전 세션 내 누적 제외 조건 (팀, IP 등)
    is_confirmed: bool            # CONFIRM 실행 여부 (True: 확정 후, False: 확정 전)


class ReclaimAgent:
    def __init__(self):
        self.llm = get_provider().as_langchain_chat_model()
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

        response = self.llm.invoke([SystemMessage(content=RECLAIM_INTENT_ANALYZER)] + history)
        raw = response.content.strip().upper()

        valid = {"START", "CONFIRM", "STATUS", "REJECT", "APPROVE", "CHAT"}
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

        response = self.llm.invoke([SystemMessage(content=build_query_constructor_prompt(intent, max_per_team))] + history)
        try:
            content = re.sub(r"```json|```", "", response.content).strip()
            plan = json.loads(content)
        except Exception:
            plan = {"filters": [], "job_status": ["READY", "IN-PROGRESS"]}

        # START 인텐트: 숫자 추출 regex fallback (LLM 오판 방지)
        if intent == "START":
            # 마지막 사용자 메시지에서 직접 숫자 추출
            last_user_content = ""
            for m in reversed(state["messages"]):
                role = m.get("role") if isinstance(m, dict) else getattr(m, "type", "")
                content_text = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
                if role in ("user", "human"):
                    last_user_content = content_text
                    break

            # "N개만", "N개로", "총/전체/IP N개" 패턴에서 total_limit 추출
            total_match = re.search(r'(?:총|전체|IP)?\s*(\d+)\s*개(?:만|로|씩)?\s*(?:하자|뽑아|회수|줄여)', last_user_content)
            if total_match:
                extracted = int(total_match.group(1))
                print(f"🔢 [Regex fallback] total_limit 추출: {extracted} (원문: '{last_user_content}')")
                plan["total_limit"] = extracted

            # "팀당 N개" 패턴에서 team_limit 추출
            team_match = re.search(r'팀당\s*(\d+)\s*개', last_user_content)
            if team_match:
                plan["team_limit"] = int(team_match.group(1))

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
                # 세션 내 누적된 제외 팀 목록 추출
                excluded_filters = state.get("excluded_filters") or []
                excluded_teams = [
                    f["value"] for f in excluded_filters
                    if f.get("target") == "owner_team" and f.get("value")
                ]
                results = repo.get_flexible_candidates(
                    team_limit=plan.get("team_limit", max_per_team),
                    total_limit=plan.get("total_limit", 20),
                    excluded_teams=excluded_teams if excluded_teams else None,
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
                item_status_filter = None
                sub_task_id_filter = None
                job_id_filter = None

                for f in plan.get("filters", []):
                    t = f.get("target")
                    if t == "item_status":
                        item_status_filter = f.get("value")
                    elif t == "sub_task_id":
                        sub_task_id_filter = f.get("value")
                    elif t == "job_id":
                        job_id_filter = f.get("value")

                # 완료된 작업도 포함 (DONE 추가)
                # 특정 작업 ID가 지정된 경우 job_status 필터 없이 전체 조회
                if sub_task_id_filter or job_id_filter:
                    job_status_filter = None
                else:
                    job_status_filter = plan.get("job_status", ["READY", "IN-PROGRESS", "DONE"])

                results = repo.get_jobs_by_filter(
                    job_id=job_id_filter,
                    sub_task_id=sub_task_id_filter,
                    item_status=item_status_filter,
                    job_status=job_status_filter,
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

    def _apply_filters_to_list(self, ip_list: List[dict], filters: List[dict]) -> List[dict]:
        """메모리 상의 IP 목록에 필터 조건을 적용해 제외 대상을 제거합니다."""
        result = []
        for ip in ip_list:
            excluded = False
            for f in filters:
                target = f.get("target")
                val = f.get("value")
                if not val:
                    continue
                if target == "owner_team" and ip.get("owner_team") == val:
                    excluded = True
                elif target == "ip_address":
                    if isinstance(val, list) and ip.get("ip_address") in val:
                        excluded = True
                    elif ip.get("ip_address") == val:
                        excluded = True
                elif target == "ip_range" and ip.get("ip_address", "").startswith(str(val)):
                    excluded = True
                elif target == "owner_email" and str(val) in ip.get("owner_email", ""):
                    excluded = True
                if excluded:
                    break
            if not excluded:
                result.append(ip)
        return result

    # ───────────────────────────────────────────────
    # [NODE 5] reject_handler
    # ───────────────────────────────────────────────
    def reject_handler(self, state: AgentState):
        """
        확정 전: selected_ips 메모리 목록에서 조건에 맞는 항목을 제거 후 재표시
        확정 후: ip_reclaim_job_item 의 item_status 를 REJECTED 로 DB 업데이트
        """
        print("🚀 [NODE: reject_handler]")
        plan = state.get("query_plan", {})
        filters = plan.get("filters", [])
        criteria = ", ".join([f"{f['target']}: {f['value']}" for f in filters if f.get("value")])

        is_confirmed = state.get("is_confirmed", False)

        if not is_confirmed:
            # ── 확정 전: 메모리 목록에서만 제거 ──
            current_ips = state.get("selected_ips", [])
            if not current_ips:
                msg = (
                    "IPAM AI Assistant입니다. "
                    "제외할 대상 목록이 없습니다. "
                    "먼저 금일 회수 대상을 조회한 후 제외 요청을 해주세요."
                )
                return {"messages": [AIMessage(content=msg)]}

            filtered_ips = self._apply_filters_to_list(current_ips, filters)
            excluded_count = len(current_ips) - len(filtered_ips)

            # 세션 내 제외 조건 누적 (이후 START 재조회에도 반영되도록)
            prev_excluded = state.get("excluded_filters") or []
            accumulated = prev_excluded + [f for f in filters if f.get("value")]

            msg = (
                f"IPAM AI Assistant입니다. "
                f"조건({criteria})에 해당하는 {excluded_count}건을 목록에서 제외했습니다. "
                f"아직 확정 전이므로 DB에는 반영되지 않습니다.\n\n"
                f"확정하시려면 '확정해줘'라고 말씀해 주세요."
            )
            return {
                "selected_ips": filtered_ips,
                "excluded_filters": accumulated,
                "messages": [AIMessage(content=msg)],
            }

        else:
            # ── 확정 후: DB 에 REJECTED 반영 ──
            db = SessionLocal()
            try:
                repo = JobRepository(db)
                count = repo.bulk_update_item_status_by_filters(filters, "REJECTED")
                msg = (
                    f"IPAM AI Assistant입니다. "
                    f"요청하신 조건({criteria})에 해당하는 대상 {count}건을 "
                    f"이번 회수 작업에서 제외 처리하였습니다."
                )
                return {"messages": [AIMessage(content=msg)]}
            finally:
                db.close()

    # ───────────────────────────────────────────────
    # [NODE 6] approve_handler (APPROVE)
    # ───────────────────────────────────────────────
    def approve_handler(self, state: AgentState):
        """
        담당자 메일 승인 처리.
        - query_plan 의 filters 에 IP 목록이 있으면 해당 IP만 승인 응답
        - filters 가 없으면 전체 IN-PROGRESS 대상 승인 응답
        - 실제 DB 상태 변경 없음 (IN-PROGRESS 유지 → 이후 /scheduler/dhcp 에서 처리)
        """
        print("🚀 [NODE: approve_handler]")
        plan = state.get("query_plan", {})
        filters = plan.get("filters", [])

        # filters 에서 IP 목록 추출
        target_ips = []
        for f in filters:
            if f.get("target") == "ip_address":
                val = f.get("value")
                if isinstance(val, list):
                    target_ips.extend(val)
                elif val:
                    target_ips.append(val)

        if target_ips:
            ip_list = ", ".join(target_ips)
            msg = (
                f"IPAM AI Assistant입니다. "
                f"{ip_list} 승인 처리되었습니다. "
                f"11:00 DHCP 회수 스케줄에 포함됩니다."
            )
        else:
            msg = (
                "IPAM AI Assistant입니다. "
                "승인 처리되었습니다. "
                "전체 IN-PROGRESS 대상이 11:00 DHCP 회수 스케줄에 포함됩니다."
            )
        return {"messages": [AIMessage(content=msg)]}

    # ───────────────────────────────────────────────
    # [NODE 7] task_executor (CONFIRM)
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
            repo.create_reclaim_job(
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

            return {"messages": [AIMessage(content="\n".join(lines))], "is_confirmed": True}

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

        response = self.llm.invoke([SystemMessage(content=build_reclaim_responder_prompt(intent, data)), HumanMessage(content="위 데이터를 바탕으로 보고해주세요.")])
        return {"messages": [AIMessage(content=response.content)]}
        return {"messages": [AIMessage(content=response.content)]}

    # ───────────────────────────────────────────────
    # [NODE 8] chat_responder (CHAT / fallback)
    # ───────────────────────────────────────────────
    def chat_responder(self, state: AgentState):
        """일반 대화 응답"""
        print("🚀 [NODE: chat_responder]")
        history = self._convert_to_messages(state["messages"])
        response = self.llm.invoke([SystemMessage(content=RECLAIM_CHAT_RESPONDER)] + history)
        return {"messages": [AIMessage(content=response.content)]}
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
    workflow.add_node("approver", agent.approve_handler)
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
            "APPROVE": "constructor",
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
            "REJECT":  "rejecter",
            "APPROVE": "approver",
            "START":   "fetcher",
            "STATUS":  "fetcher",
        },
    )

    # 각 핸들러 완료 후 dispatcher로 복귀 (다음 인텐트 처리)
    workflow.add_edge("fetcher",       "responder")
    workflow.add_edge("responder",     "dispatcher")
    workflow.add_edge("rejecter",      "dispatcher")
    workflow.add_edge("approver",      "dispatcher")
    workflow.add_edge("executor",      "dispatcher")
    workflow.add_edge("chat_responder", END)

    return workflow.compile()


reclaim_graph = build_reclaim_graph()
