from langgraph.graph import StateGraph, END
from .shared_state import AgentState
from langchain_core.messages import AIMessage

def candidate_dummy(state: AgentState):
    return {"messages": [AIMessage(content="[후보 추출 에이전트] 아직 개발 중인 기능입니다. 동혁님의 '회수 진행' 기능을 테스트해보세요!")]}

# 빈 그래프 생성
builder = StateGraph(AgentState)
builder.add_node("candidate_work", candidate_dummy)
builder.set_entry_point("candidate_work")
builder.add_edge("candidate_work", END)
candidate_graph = builder.compile()