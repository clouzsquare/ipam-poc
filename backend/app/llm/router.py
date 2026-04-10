import os
from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI
from .shared_state import AgentState
from .reclaim_agent import reclaim_graph     # 동혁님 에이전트
from .candidate_agent import candidate_graph # 우현님 에이전트

class MasterOrchestrator:
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash", 
            temperature=0
        )
    def route_decision(self, state: AgentState):
        """질문 내용을 보고 도메인을 분류합니다."""
        # 💡 마지막 메시지 추출
        last_msg = state["messages"][-1]
        
        # 💡 [수정] 객체(.content)와 딕셔너리(['content']) 모두 대응 가능하도록 처리
        if hasattr(last_msg, 'content'):
            last_msg_text = last_msg.content
        else:
            last_msg_text = last_msg.get('content', '')
        
        prompt = f"""
        사용자의 질문을 분석하여 어떤 전문가에게 연결할지 결정하세요.
        질문: "{last_msg_text}"

        - CANDIDATE: IP 회수 대상 후보를 추출하거나 선정하는 것과 관련된 질문
        - RECLAIM: 이미 뽑힌 목록을 확정(진행)하거나, 현재 진행 중인 작업 현황 조회
        - CHAT: 단순 인사 또는 기타

        오직 한 단어(CANDIDATE, RECLAIM, CHAT)로만 대답하세요.
        """
        res = self.llm.invoke(prompt)
        domain_raw = res.content.strip().upper()
        
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

# 💡 드디어 chat.py에서 기다리던 그 녀석!
master_graph = MasterOrchestrator().build_master_graph()