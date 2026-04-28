import os
from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage, SystemMessage
from .shared_state import AgentState
from .reclaim_agent import reclaim_graph     # 동혁님 에이전트
from .candidate_agent import candidate_graph # 우현님 에이전트
from .provider import get_provider

class MasterOrchestrator:
    def __init__(self):
        self.llm = get_provider().as_langchain_chat_model()
    def route_decision(self, state: AgentState):
        """질문 내용을 보고 도메인을 분류합니다."""
        # 마지막 메시지 추출
        last_msg = state["messages"][-1]
        
        # 객체(.content)와 딕셔너리(['content']) 모두 대응 가능하도록 처리
        if hasattr(last_msg, 'content'):
            last_msg_text = last_msg.content
        else:
            last_msg_text = last_msg.get('content', '')

        # 전체 대화 이력을 컨텍스트로 구성
        transcript_lines = []
        for message in state.get("messages", []):
            if hasattr(message, "content"):
                content = message.content
                role = getattr(message, "type", "assistant")
            else:
                content = message.get("content", "")
                role = message.get("role", "assistant")
            transcript_lines.append(f"{str(role).upper()}: {str(content)}")
        transcript = "\n".join(transcript_lines)
        
        prompt = f"""
        당신은 IPAM 시스템의 AI Assistant 입니다. 사용자의 요청을 분석하여 적절한 에이전트에게 배분하세요.

        1. CANDIDATE (차수별 IP회수 후보 추출 및 확정 — 3~4개월에 한 번):
           - 역할: 엑셀 파일을 업로드하여 IP 회수 후보를 추출하거나, 추출된 후보를 DB에 확정하는 작업.
           - 해당하는 경우:
             · 엑셀 파일 업로드와 함께 후보 추출/확정 요청
             · "후보 추출해줘", "후보 뽑아줘", "이번 차수 후보 확정" 등 차수 단위 작업
             · "후보 검토 메일 보내줘" (담당자에게 검토 요청)
           - 핵심: 엑셀 파일 기반 / 차수 단위 / 신규 후보 도출

        2. RECLAIM (일일 IP 회수 운영 — 매일):
           - 역할: 이미 DB에 등록된 후보들 중 오늘 실제로 회수할 목록을 관리하고 실행하는 작업.
           - 해당하는 경우:
             · "오늘 회수할 대상 알려줘", "금일 작업 목록", "오늘 IP 회수 시작"
             · "오늘 회수할 후보들 알려줘" (DB에 있는 후보 중 오늘치 선택)
             · 특정 IP/팀 제외, 개수 조정, 작업 확정, 담당자 메일, NTOSS 연동
             · 진행 현황, DHCP/장비 회수 결과 조회
           - 핵심: DB 기반 / 일일 단위 / 기존 후보 중 선택 및 실행

        3. CHAT: 단순 인사, 시스템 사용법 문의 등

        [핵심 구분 원칙]
        - "후보"라는 단어만으로 CANDIDATE 판단 금지
        - "오늘", "금일", "일일" 키워드가 있으면 → RECLAIM
        - 엑셀 파일 업로드 또는 "이번 차수", "차수별" 맥락이면 → CANDIDATE
        - 모호한 경우 직전 대화 흐름 우선 판단:
          · 직전 흐름이 엑셀 업로드/차수별 작업 → CANDIDATE
          · 직전 흐름이 오늘 작업/NTOSS/현황 조회 → RECLAIM

        [대화 이력]
        {transcript}

        [마지막 사용자 요청]
        {last_msg_text}

        오직 한 단어(CANDIDATE, RECLAIM, CHAT)로만 대답하세요. 설명 금지.
        """
        res = self.llm.invoke([SystemMessage(content=prompt), HumanMessage(content=last_msg_text)])
        domain_raw = str(res.content).strip().upper()
        
        if "RECLAIM" in domain_raw: domain = "reclaim"
        elif "CANDIDATE" in domain_raw: domain = "candidate"
        else: domain = "chat"
        
        return {"domain": domain}

    def build_master_graph(self):
        workflow = StateGraph(AgentState)
        
        # 1. 노드 등록
        workflow.add_node("router", self.route_decision)
        workflow.add_node("reclaim_domain", reclaim_graph)
        workflow.add_node("candidate_domain", candidate_graph)

        # 2. 시작점 설정
        workflow.set_entry_point("router")
        
        # 3. 조건부 라우팅 설정
        workflow.add_conditional_edges(
            "router",
            lambda x: x["domain"],
            {
                "reclaim": "reclaim_domain",
                "candidate": "candidate_domain",
                "chat": END # 일반 채팅은 여기서 종료 (혹은 별도 노드 추가)
            }
        )
        
        workflow.add_edge("reclaim_domain", END)
        workflow.add_edge("candidate_domain", END)
        
        return workflow.compile()

# 드디어 chat.py에서 기다리던 그 녀석!
master_graph = MasterOrchestrator().build_master_graph()