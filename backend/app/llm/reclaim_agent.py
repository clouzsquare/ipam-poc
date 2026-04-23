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

        system_prompt = """
당신은 IPAM AI Assistant의 의도 분류기입니다.
사용자 요청에서 수행해야 할 작업을 실행 순서대로 추출하세요.

[의도 종류]
- REJECT  : 특정 IP, 팀, 대역 등 구체적인 대상을 작업에서 제외 요청 ("빼줘", "제외", "취소", "불가", "반대", "보류" 등)
- APPROVE : IP 회수 작업을 승인 ("승인", "확인", "동의", "진행해주세요", "괜찮습니다" 등) — 담당자 메일 회신에서 주로 발생
- START   : 금일 회수할 IP 후보 목록을 뽑아달라는 요청. 다시 뽑기, 조건 변경 후 재조회 포함
- CONFIRM : 준비된 목록을 확정하고 메일 발송 및 NTOSS 등록
- STATUS  : 이미 확정된 작업의 DHCP/장비 회수 진행 현황 조회
- CHAT    : 단순 인사, 사용법 문의 등

[START vs STATUS 핵심 구분]
- START : 아직 확정 전, 오늘 작업 후보를 뽑는 단계
  → "오늘 회수 대상 알려줘", "목록 알려줘", "다시 뽑아줘", "목록 다시 알려줘", "N개만 하자"
- STATUS : 이미 확정(CONFIRM)된 작업의 진행 상황 확인
  → "진행 현황 알려줘", "DHCP 결과 어때", "장비 회수 상태", "실패한 거 있어"

[START 개수/조건 변경]
- "N개만 하자", "전체 N개로 줄여줘", "팀당 N개로" 처럼 총량·팀 제한만 바꾸는 경우 → START
- "클라우드팀 빼줘", "10.0.0.1 제외해줘" 처럼 특정 대상을 지정 → REJECT

[규칙]
- 여러 의도가 있으면 실행 순서대로 콤마로 구분 (예: REJECT,START)
- 단일 의도면 하나만 출력
- 반드시 위 목록에서만 선택
- 순수하게 콤마 구분 의도만 출력 (다른 텍스트 없이)

예시)
- "오늘 IP 회수 시작해줘" → START
- "오늘은 5개만 하자" → START
- "팀당 2개, 전체 10개로 다시 뽑아줘" → START
- "클라우드팀 빼고 목록 다시 알려줘" → REJECT,START
- "보안팀 제외하고 4건만 회수하자. 목록 다시 알려줘" → REJECT,START
- "1.1.1.1 빼고 오늘 작업 시작해줘" → REJECT,START
- "확정해줘" → CONFIRM
- "진행 현황 알려줘" → STATUS
- "DHCP 회수 결과 어때" → STATUS
- "클라우드팀은 빼줘" → REJECT
- "승인합니다" → APPROVE
- "10.0.0.1 제외해주세요. 나머지는 진행해주세요." → REJECT
- "확인했습니다. 진행해주세요." → APPROVE
"""
        response = self.llm.invoke([SystemMessage(content=system_prompt)] + history)
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
START 전체 개수 변경: {{"team_limit": {max_per_team}, "total_limit": 5, "action": "START"}}  ← "오늘은 5개만 하자" / "5개만 뽑아줘" / "IP 5개만 회수"
START 팀당 개수 변경: {{"team_limit": 3, "total_limit": 15, "action": "START"}}  ← "팀당 3개, 15개로 시작"
REJECT: {{"filters": [{{"target": "owner_team", "value": "클라우드팀"}}, {{"target": "ip_address", "value": ["10.0.0.1"]}}], "action": "REJECT"}}
STATUS 전체: {{"job_status": ["READY", "IN-PROGRESS", "DONE"], "filters": [], "action": "STATUS"}}
STATUS 장애: {{"filters": [{{"target": "item_status", "value": ["DHCP_FAILED", "DEVICE_FAILED"]}}], "job_status": ["READY", "IN-PROGRESS", "DONE"], "action": "STATUS"}}
STATUS 특정 서브작업: {{"filters": [{{"target": "sub_task_id", "value": "NTOSS-SUB-XXXXXX"}}], "action": "STATUS"}}
STATUS 특정 메인작업: {{"filters": [{{"target": "job_id", "value": "NTOSS-MAIN-XXXXXX"}}], "action": "STATUS"}}

[START 파라미터 추출 규칙]
- "N개만", "N개로", "총 N개", "전체 N개", "IP N개" 같은 표현에서 N을 total_limit으로 추출
- "팀당 N개" 같은 표현에서 N을 team_limit으로 추출
- total_limit과 team_limit은 독립적으로 설정 (total_limit만 변경 가능)
- 명시되지 않은 값은 현재 기본값 유지 (team_limit={max_per_team}, total_limit=20)
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
- 반드시 마크다운 표(table) 형식으로 데이터를 출력할 것
- START: 건수 요약 후 아래 형식으로 출력
  | IP 주소 | NW ID | 팀 | 이메일 |
  |---|---|---|---|
  | ... | ... | ... | ... |
- STATUS: 상태별 건수 요약 후 아래 형식으로 출력
  | IP 주소 | 팀 | 상태 |
  |---|---|---|
  | ... | ... | ... |
- CSV 형식이나 일반 텍스트 목록은 사용하지 말 것
- 표 외 설명은 표 앞뒤에 간결하게 작성
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
