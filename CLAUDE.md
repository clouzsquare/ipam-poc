# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IPAM AI Reclamation Agent PoC — LG CNS 통신사업부의 IP 회수 업무를 AI 에이전트(채팅)가 자동 처리하는 시스템.

## Tech Stack

- **Frontend**: React, Tailwind CSS, Axios
- **Backend**: Python 3.11+, FastAPI, LangChain, LangGraph
- **LLM**: Google Gemini 2.5 Flash (configurable via `backend/config/llm_config.json`)
- **Database**: MySQL 8.0, SQLAlchemy ORM
- **External**: NTOSS API (Mock), Gmail SMTP

## Architecture

### Multi-Agent System

```
User (Chat UI)
    │
    ▼
POST /api/v1/chat
    │
    ▼
Master Router (LLM routing)
    ├─▶ Reclaim Agent  ─── 일일 IP 회수 (START/REJECT/APPROVE/CONFIRM/STATUS)
    └─▶ Candidate Agent ── 차수별 후보 추출 (START/UPLOAD/SENDMAIL/FINALIZE)
```

- **Master Router** (`app/llm/router.py`): 사용자 메시지를 분석해 Reclaim/Candidate로 분기
- **Reclaim Agent** (`app/llm/reclaim_agent.py`): LangGraph 9-node pipeline, 일일 작업 관리
- **Candidate Agent** (`app/llm/candidate_agent.py`): LangGraph 3-node pipeline, 차수별 후보 추출

### LLM Provider Abstraction

LLM provider는 `app/llm/provider.py`의 추상 계층으로 관리됩니다.

- 전환: `LLM_PROVIDER` 환경변수 (`gemini`/`ollama`) 또는 `config/llm_config.json`
- 새 provider 추가: `LLMProvider` 상속 → `register_provider()` 등록

## Directory Structure

```
backend/
├── app/
│   ├── api/v1/           # FastAPI endpoints (chat, scheduler, candidate)
│   ├── llm/              # AI agents, router, provider abstraction
│   ├── repositories/     # SQLAlchemy CRUD
│   ├── services/         # Business logic
│   ├── models/           # DB entities
│   ├── client/           # External clients (NtossClient)
│   └── core/             # DB config
├── config/llm_config.json
└── main.py               # FastAPI entry point

frontend/
└── ipam-frontend/   # React frontend (npm install & npm start here)
```

## Commands

```bash
# Backend
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 main.py

# Frontend
cd frontend
npm install
npm start

# Root (both)
npm run dev
```

## Key Conventions

- **Indentation**: 2 spaces
- **Naming**: React components PascalCase, variables/functions camelCase
- **Commits**: Conventional Commits (`type(scope): description`)
- **LLM prompts**: Korean — system prompts in agents expect Korean input/output

## State Architecture

**Master Router** uses `shared_state.AgentState`:
```
messages: Annotated[List[BaseMessage], operator.add]
domain: str  # "candidate" | "reclaim" | "chat"
query_plan: dict
selected_ips: List[dict]
```

**Reclaim/Candidate Agents** each define their own `AgentState` (TypedDict) inside the agent file — not shared.

## LLM Provider (`app/llm/provider.py`)

- `get_provider()` → `LLMProvider` subclass via registry
- `as_langchain_chat_model()` wraps provider in `SimpleChatModel`
- **Important**: `_call()` must return `str`, not `ChatResult` — extracted via `result.messages[0].content`
- Ollama models: set via `config/llm_config.json` or `LLM_PROVIDER=ollama`

## Testing

```bash
# Backend test (server must be running)
cd backend
python3 main.py  # terminal 1
python3 test_integration.py  # terminal 2
```

## Critical Business Rules

- **확정 전/후 구분**: `is_confirmed` 플래그로 관리. 확정 전 제외는 메모리만, 확정 후 제외는 DB 반영
- **팀당/ip당 균등 배분**: `ip_reclaim_candidate` 조회 시 윈도우 함수로 팀당 할당
- **복합 인텐트**: 하나의 메시지에 REJECT,START 등 여러 인텐트가 순서대로 처리될 수 있음
- **NTOSS/Gmail**: 현재 Mock 구현 — `NtossClient`, `gmail_service` 내부만 교체하면 실제 연동 전환 가능