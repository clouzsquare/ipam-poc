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
        # 마지막 메시지 추출
        last_msg = state["messages"][-1]
        
        # 객체(.content)와 딕셔너리(['content']) 모두 대응 가능하도록 처리
        if hasattr(last_msg, 'content'):
            last_msg_text = last_msg.content
        else:
            last_msg_text = last_msg.get('content', '')
        
        prompt = f"""
        당신은 IPAM 시스템의 AI Assistant 입니다. 사용자의 요청을 분석하여 적절한 에이전트에게 배분하세요.

        1. CANDIDATE (IP회수 후보 추출 및 확정):
           - 역할: 사용자로부터 입력된 엑셀 파일에서 정해진 룰에 따라 IP회수 후보를 추출하고 확정하는 모든 작업.
           - 주요 업무: 
             ① 회수 후보 추출 : 사용자가 'NW ID별 IP대역 사용률' 엑셀 파일을 업로드하면, 파일 내용을 분석하여 IP회수 후보를 추출.
             ② 회수 후보 담당자 검토 요청 메일 발송 : 추출된 후보를 담당자에게 검토 요청 메일로 발송
             ③ 회수 후보 확정 : 담당자 검토를 마친 'NW ID별 IP대역 사용률' 엑셀 파일을 업로드하면, 최종 회수 후보를 확정하여 DB에 반영
           - 키워드: "회수 후보 추출", "회수 후보 확정", "회수 후보 목록 조회", "후보 추출 시작", "후보 메일 발송", "후보 확정해줘", "후보 확정", "후보 다시 뽑아줘", "후보 현황 알려줘" 등

        2. RECLAIM (일단위 운영 및 실행):
           - 역할: 추출된 후보를 바탕으로 '오늘' 실제로 수행하는 모든 액션.
           - 주요 업무: 
             ① 금일 작업 대상 확정 및 추출 
             ② 담당자 검토 요청 메일 발송 (In-Progress 전이)
             ③ NTOSS 연동을 통한 실제 회수 실행
             ④ 회수 중 발생한 장애(DHCP/Device 에러) 대응 및 재시도
             ⑤ 실시간 진행 현황 및 통계 조회
           - 키워드: "오늘 작업 시작", "메일 보내줘", "확정해", "진행 현황 어때", "10.x.x.x는 빼줘", "실패한 거 왜이래?"

        3. CHAT: 단순 인사, 시스템 사용법 문의 등

        요청: "{last_msg}"
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

# 드디어 chat.py에서 기다리던 그 녀석!
master_graph = MasterOrchestrator().build_master_graph()