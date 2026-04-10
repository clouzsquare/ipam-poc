import operator
from typing import TypedDict, List, Annotated, Union
from langchain_core.messages import BaseMessage

class AgentState(TypedDict):
    # 대화 기록은 모든 에이전트가 공유
    messages: Annotated[List[BaseMessage], operator.add]
    # 어떤 에이전트가 처리할지 결정 (candidate / reclaim / chat)
    domain: str 
    # 동혁님이 설계하신 Query Plan
    query_plan: dict
    # 추출된 리스트 (Candidate 에이전트가 채우고, Reclaim 에이전트가 사용)
    selected_ips: List[dict]