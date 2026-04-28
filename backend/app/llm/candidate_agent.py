import operator
import os
from typing import Annotated, List, TypedDict, Union

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from app.llm.prompts import CANDIDATE_INTENT_CLASSIFIER
from app.services.candidate_service import CandidateService
from app.llm.provider import get_provider


class AgentState(TypedDict):
    messages: Annotated[List[Union[dict, BaseMessage]], operator.add]
    intent: str
    query_plan: dict
    selected_ips: List[dict]


class CandidateAgent:
    def __init__(self):
        load_dotenv()
        self.llm = get_provider().as_langchain_chat_model()
        self.candidate_service = CandidateService()
        self.demo_review_recipient = os.getenv("CANDIDATE_DEMO_REVIEW_EMAIL", "").strip()

    def _convert_to_messages(self, messages: List[Union[dict, BaseMessage]]) -> List[BaseMessage]:
        converted: List[BaseMessage] = []
        for message in messages:
            if isinstance(message, BaseMessage):
                converted.append(message)
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            converted.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
        return converted

    def intent_analyzer(self, state: AgentState):
        print("\n🚀 [NODE: intent_analyzer(Candidate)]")
        history = self._convert_to_messages(state["messages"])
        res = self.llm.invoke([SystemMessage(content=CANDIDATE_INTENT_CLASSIFIER)] + history)
        raw = str(res.content).upper()
        intent = next(
            (x for x in ["FINALIZE", "SENDMAIL", "UPLOAD", "REJECT", "START", "CHAT"] if x in raw),
            "CHAT",
        )
        print(f"🎯 분석된 Intent(Candidate): {intent}")
        return {"intent": intent}

    def action_planner(self, state: AgentState):
        print("\n🚀 [NODE: action_planner(Candidate)]")
        intent = state.get("intent", "CHAT")
        intent_to_action = {
            "START": "GUIDE_UPLOAD",
            "UPLOAD": "WAIT_UPLOAD_PROCESS",
            "SENDMAIL": "SEND_REVIEW_MAIL",
            "FINALIZE": "GUIDE_FINALIZE_UPLOAD",
            "REJECT": "ASK_MORE_TARGET",
            "CHAT": "CHAT",
        }
        action = intent_to_action.get(intent, "CHAT")
        print(f"🎯 분석된 Action(Candidate): {action}")
        return {"query_plan": {"action": action}}

    def responder(self, state: AgentState):
        print("🚀 [NODE: responder(Candidate)]")
        action = state.get("query_plan", {}).get("action", "CHAT")
        print(f"🎯 Action(Candidate): {action}")

        if action == "GUIDE_UPLOAD":
            return {
                "messages": [
                    AIMessage(
                        content="IP회수 후보 목록 추출을 시작합니다. NW ID별 IP대역 사용률 엑셀파일을 업로드해 주세요."
                    )
                ]
            }

        if action == "WAIT_UPLOAD_PROCESS":
            return {
                "messages": [
                    AIMessage(
                        content="엑셀 업로드를 확인했습니다. 대화 State를 기준으로 추출용/확정용 업로드를 판별해 처리합니다."
                    )
                ]
            }

        if action == "SEND_REVIEW_MAIL":
            selected_ips = state.get("selected_ips", [])
            if not selected_ips:
                return {"messages": [AIMessage(content="검토 메일을 보낼 후보 목록이 없습니다. 먼저 엑셀 업로드를 진행해 주세요.")]}
            mail_result = self.candidate_service.send_review_mails(
                selected_ips=selected_ips,
                override_recipients=[self.demo_review_recipient] if self.demo_review_recipient else None,
            )
            if mail_result.get("failed"):
                failed_reasons = mail_result.get("failed_reasons", {})
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                f"검토 요청 메일 발송 중 일부 실패가 발생했습니다. "
                                f"성공 {mail_result.get('sent_count', 0)}건, 실패 {mail_result.get('failed')}, "
                                f"사유 {failed_reasons}"
                            )
                        )
                    ]
                }
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"검토 요청 메일 발송이 완료되었습니다. "
                            f"수신자: {self.demo_review_recipient or '자동 계산 대상'}, 발송 건수: {mail_result.get('sent_count', 0)}건"
                        )
                    )
                ]
            }

        if action == "GUIDE_FINALIZE_UPLOAD":
            return {
                "messages": [
                    AIMessage(
                        content="IP회수 후보 목록 확정을 진행합니다. 인프라 담당자 검토가 반영된 엑셀파일을 업로드해 주세요."
                    )
                ]
            }

        if action == "ASK_MORE_TARGET":
            return {"messages": [AIMessage(content="알겠습니다. 회수 후보를 다시 추출하겠습니다. 엑셀파일을 다시 업로드해 주세요.")]}

        return {"messages": [AIMessage(content="후보 추출 관련 문의를 도와드릴게요.")]}


def build_candidate_graph():
    agent = CandidateAgent()
    workflow = StateGraph(AgentState)
    workflow.add_node("analyzer", agent.intent_analyzer)
    workflow.add_node("constructor", agent.action_planner)
    workflow.add_node("responder", agent.responder)

    workflow.set_entry_point("analyzer")
    workflow.add_edge("analyzer", "constructor")
    workflow.add_conditional_edges(
        "constructor",
        lambda x: x.get("query_plan", {}).get("action", "CHAT"),
        {
            "GUIDE_UPLOAD": "responder",
            "WAIT_UPLOAD_PROCESS": "responder",
            "SEND_REVIEW_MAIL": "responder",
            "GUIDE_FINALIZE_UPLOAD": "responder",
            "ASK_MORE_TARGET": "responder",
            "CHAT": "responder",
        },
    )
    workflow.add_edge("responder", END)
    return workflow.compile()


candidate_graph = build_candidate_graph()