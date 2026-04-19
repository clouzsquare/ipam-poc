# IPAM AI Reclamation Agent PoC — 상세 설명서

## 1. 프로젝트 개요

LG CNS 통신 사업부의 IP 회수 업무는 현재 담당자가 수작업으로 처리하고 있습니다. 본 PoC는 이 업무 전체를 **AI 에이전트**가 대화(Chat) 형태로 처리하도록 자동화한 시스템입니다.

사용자는 별도의 메뉴나 버튼 없이 자연어로 지시하고, AI가 판단·실행·결과 보고까지 수행합니다.

---

## 2. IP 회수 업무 흐름

IP 회수는 두 단계로 구성됩니다.

```
[차수별 작업 — 3~4개월에 1회]
엑셀 업로드 → AI가 후보 추출 → 담당자 검토 메일 → 후보 확정 → DB 등록

[일일 작업 — 매일]
후보 중 오늘 작업 목록 선정 → 담당자 안내 메일 → DHCP 회수 → 장비 회수 → 완료
```

---

## 3. 시스템 구성

### 3.1 전체 아키텍처

```
사용자 (채팅 UI)
    │
    ▼
POST /api/v1/chat
    │
    ▼
Master Router (LLM)
    ├─▶ Reclaim Agent  ─── 일일 IP 회수 업무
    └─▶ Candidate Agent ── 차수별 후보 추출 업무
         │
         ▼
    MySQL (SQLAlchemy ORM)
    NTOSS API (Mock)
    Gmail SMTP
```

### 3.2 기술 스택

| 구분 | 기술 |
|------|------|
| Frontend | React, Tailwind CSS, react-markdown |
| Backend | Python 3.11, FastAPI |
| AI Framework | LangChain, LangGraph |
| LLM | Google Gemini 2.5 Flash |
| Database | MySQL 8.0, SQLAlchemy ORM |
| 외부 연동 | NTOSS API (Mock), Gmail SMTP |

---

## 4. AI 에이전트 상세 설명

### 4.1 Master Router

사용자 메시지와 대화 이력을 분석해 어느 에이전트로 보낼지 결정합니다.

| 도메인 | 라우팅 기준 | 처리 에이전트 |
|--------|-------------|---------------|
| RECLAIM | "오늘", "금일", "진행 현황", 일일 작업 관련 | Reclaim Agent |
| CANDIDATE | "차수", 엑셀 업로드, 후보 추출/확정 관련 | Candidate Agent |
| CHAT | 인사, 사용법 문의 | 직접 응답 |

단순 키워드 매칭이 아닌 **대화 흐름 전체**를 문맥으로 사용하여 판단합니다.

---

### 4.2 Reclaim Agent (일일 IP 회수)

LangGraph 기반으로 9개 노드가 파이프라인으로 연결됩니다.

#### 인텐트(의도) 분류

하나의 메시지에서 여러 의도를 동시에 추출하고 순서대로 처리합니다.

| 인텐트 | 설명 | 예시 |
|--------|------|------|
| START | 오늘 회수할 IP 목록 조회 | "오늘 IP 회수 대상 알려줘" |
| REJECT | 특정 대상 제외 | "클라우드팀 빼줘" |
| APPROVE | 담당자 메일 승인 | "승인합니다" |
| CONFIRM | 목록 확정 + 메일 발송 + NTOSS 등록 | "확정해줘" |
| STATUS | 작업 진행 현황 조회 | "DHCP 회수 결과 어때" |
| CHAT | 일반 대화 | "이 시스템 어떻게 써?" |

**복합 처리 예시:**
> "클라우드팀 빼고 오늘은 5개만 하자. 목록 다시 알려줘"
> → `REJECT` → `START` 순서로 처리

#### 노드 구성 및 흐름

```
[analyzer]
  의도 추출 (다중 인텐트 큐 구성)
       │
  [dispatcher]
  큐에서 현재 인텐트 꺼냄
       │
  ┌────┴─────────────────────────┐
  │                              │
[constructor]              [executor]  ← CONFIRM
  쿼리 플랜 생성               NTOSS + DB + 메일
  │
  ├─ REJECT  → [rejecter]   → dispatcher
  ├─ APPROVE → [approver]   → dispatcher
  ├─ START   → [fetcher] → [responder] → dispatcher
  └─ STATUS  → [fetcher] → [responder] → dispatcher

[chat_responder] ← CHAT → END
[dispatcher] → DONE → END
```

---

### 4.3 일일 IP 선정 로직

DB의 `ip_reclaim_candidate` 테이블에서 균등하게 대상을 추출합니다.

- **기본 조건**: 전체 최대 20개 / 팀당 최대 4개
- **사용자 조정 가능**: 대화 중 "5개만 하자", "팀당 2개로" 등으로 변경
- **균등 배분**: MySQL 윈도우 함수(`ROW_NUMBER() OVER PARTITION BY owner_team`)로 특정 팀에 몰리지 않도록 처리
- **제외 조건 누적**: 세션 내 제외한 팀/IP는 재조회 시에도 자동 반영

---

### 4.4 확정 전 / 확정 후 구분

**확정(CONFIRM) 전**: 조회된 목록은 프론트엔드 메모리에만 존재. 제외 요청 시 DB 변경 없이 메모리 목록에서만 제거.

**확정(CONFIRM) 후**: DB에 `ip_reclaim_job`, `ip_reclaim_job_item` 생성. 제외 요청 시 실제 DB에 `REJECTED` 반영.

이 전환점은 `is_confirmed` 플래그로 관리되며, 프론트엔드 ↔ 백엔드 간 매 요청마다 왕복합니다.

---

### 4.5 Candidate Agent (차수별 후보 추출)

3~4개월에 한 번씩 수행하는 차수별 IP 회수 후보 추출 업무를 담당합니다. LangGraph 기반 3개 노드로 구성되며, 채팅 + 엑셀 업로드를 통해 후보를 추출하고 인프라팀에 검토 메일을 발송합니다.

#### 전체 흐름

```
엑셀 업로드 (NW별 IP 사용률 데이터)
    │
    ▼
후보 추출 (사용률 < 기준치 + Non-primary + 의미 기반 제외)
    │
    ▼
검토 엑셀 다운로드 + 인프라팀 담당자 검토 요청 메일 발송
    │
    ▼
검토 완료 엑셀 재업로드 → DB 확정 반영
```

#### 인텐트(의도) 분류

| 인텐트 | 설명 | 에이전트 응답 |
|--------|------|--------------|
| START | 후보 추출 프로세스 시작 요청 | 엑셀 업로드 안내 |
| UPLOAD | 엑셀 파일 업로드 감지 | 추출/확정 자동 판별 처리 |
| SENDMAIL | 추출 결과 검토 메일 발송 요청 | 인프라팀에 Gmail 발송 |
| REJECT | 추출 결과 재실행 요청 | 엑셀 재업로드 안내 |
| FINALIZE | 후보 확정 프로세스 시작 | 확정용 엑셀 업로드 안내 |
| CHAT | 그 외 일반 대화 | 안내 응답 |

#### 노드 구성

```
[intent_analyzer]
  6개 인텐트 분류
       │
  [action_planner]
  인텐트 → 액션 매핑
       │
  [responder]
  액션별 응답 처리 (메일 발송 포함)
       │
      END
```

#### IP 후보 선정 기준 (3가지 조건 AND)

| 조건 | 내용 | 기본값 |
|------|------|--------|
| 사용률 미달 | IP 사용률 < 임계치 | 30% (환경변수 조정 가능) |
| Non-primary | `Primary 여부` 컬럼이 `Y`가 아닌 대상 | 고정 |
| 의미 기반 제외 아님 | 네트워크명·NTOSS 단지명이 제외 카테고리에 해당하지 않음 | LLM 판단 |

**의미 기반 제외 카테고리** (LLM이 네트워크명/단지명을 분류):

| 카테고리 | 제외 대상 예시 |
|----------|--------------|
| 단기 숙박·숙소형 시설 | 기숙사, 호텔, 모텔, 리조트, 게스트하우스 |
| 휴대폰·통신 대리점 | OO텔레콤, 휴대폰 대리점, 공식대리점 |

> 카테고리는 `candidate_reclaim_policy.py`의 `SEMANTIC_EXCLUSION_CATEGORIES`에 항목을 추가하여 확장 가능합니다.

#### 엑셀 업로드 모드 자동 판별

업로드 시 **대화 이력 전체를 LLM에 전달**하여 현재 단계가 "추출"인지 "확정"인지 자동 판별합니다.

| 모드 | 판별 기준 | 동작 |
|------|-----------|------|
| EXTRACT | 기본값 또는 추출 단계 대화 맥락 | 사용률 분석 → 후보 추출 → 검토 엑셀 생성 |
| FINALIZE | 대화에 "확정", "반영" 등 포함 | 엑셀의 모든 행을 DB(`ip_reclaim_candidate`)에 INSERT |

#### 검토 메일 발송 로직

- **수신자 결정**: `INFRA_TEAM_EMAIL_MAP` 환경변수(팀명→이메일 매핑) 우선 적용. 미설정 시 각 IP의 `owner_email` 사용.
- **첨부 파일**: 후보 항목과 회수 사유가 기재된 검토용 엑셀 자동 생성·첨부.
- **데모 모드**: `CANDIDATE_DEMO_REVIEW_EMAIL` 설정 시 모든 메일이 해당 주소로 집중 발송 (데모 시연용).
- **Gmail 미설정 시**: 실제 발송 없이 성공으로 처리 (Mock 모드).

#### 엑셀 필수 컬럼

`DHCP Server IP`, `IP블록`, `인프라팀`, `네트워크 이름`, `네트워크 ID`, `Primary 여부`, `사용률(%)`

누락 시 400 에러 반환.

#### API 엔드포인트

| 엔드포인트 | 설명 |
|-----------|------|
| `POST /api/v1/candidate/upload` | 대화 이력 기반 자동 모드 판별 업로드 |
| `POST /api/v1/candidate/extract` | 추출 전용 (모드 명시) |
| `POST /api/v1/candidate/finalize` | 확정 전용 (모드 명시) |

채팅 UI에서는 `/candidate/upload`만 사용하며, 대화 이력을 함께 전송해 모드를 자동 결정합니다.

#### 주요 환경변수

| 변수명 | 설명 | 기본값 |
|--------|------|--------|
| `CANDIDATE_USAGE_THRESHOLD` | 회수 후보 사용률 임계치 (%) | `30` |
| `CANDIDATE_DEFAULT_OWNER_EMAIL` | 담당자 이메일 미지정 시 폴백 | `no-reply@ipam.local` |
| `INFRA_TEAM_EMAIL_MAP` | 팀명→이메일 JSON 매핑 | 없음 |
| `CANDIDATE_DEMO_REVIEW_EMAIL` | 검토 메일 단일 수신처 (데모용) | 없음 |

---

## 5. 데이터베이스 구조

### 5.1 테이블 관계

```
ip_reclaim_candidate (회수 후보 원본)
    └── 차수별 작업에서 생성, status: READY → IN_PROGRESS

ip_reclaim_job (일일 작업 메인)
    └── NTOSS 작업 ID 보관, job_status: READY → IN-PROGRESS → DONE

ip_reclaim_job_item (IP별 처리 현황)
    └── ip_reclaim_job 1:N 관계
```

### 5.2 ip_reclaim_job_item 상태 전이

```
IN-PROGRESS
    │
    ▼ (11:00 /scheduler/dhcp)
DHCP_SUCCESS ─── DHCP_FAILED
    │                  │
    │              no_action() + 관리자 메일
    ▼
(17:00 /scheduler/device)
DEVICE_SUCCESS ── DEVICE_FAILED
    │                  │
    ▼              IP 재할당 + 관리자 메일
  DONE

REJECTED: 담당자 거부 또는 사용자 수동 제외
```

---

## 6. 스케줄러 (일일 자동화 파이프라인)

PoC에서는 실제 스케줄러 대신 API 엔드포인트로 제공하여 시연 시 Postman 등으로 직접 호출합니다.

| 시간 | 엔드포인트 | 동작 |
|------|------------|------|
| 09:00 | CONFIRM 처리 (채팅) | 담당자에게 사전 안내 메일 발송 |
| 11:00 | POST /scheduler/dhcp | NTOSS DHCP 서버 IP 반납 처리 |
| 17:00 | POST /scheduler/device | NTOSS 장비 IP 삭제 처리 |
| 수시 | POST /scheduler/mail-reply | 담당자 메일 회신 처리 |

### 오류 처리

**DHCP 실패 시:**
1. NTOSS `no_action()` 호출 (작업없음 처리)
2. 관리자에게 오류 내용 메일 발송
3. 해당 IP → `DHCP_FAILED` 상태

**장비 회수 실패 시:**
1. 신규 서브작업 생성 (원복용)
2. NTOSS `allocate_ip()` 호출 (IP 재할당/원복)
3. 원복 서브작업 완료 처리
4. 기존 서브작업 `no_action()` 처리
5. 관리자에게 오류 내용 메일 발송
6. 해당 IP → `DEVICE_FAILED` 상태

---

## 7. NTOSS 연동 (Mock)

현재 모든 NTOSS 호출은 Mock으로 구현되어 있으며, 실제 API 연동 시 `NtossClient` 내부 구현만 교체하면 됩니다.

| API | 설명 | 실패율 |
|-----|------|--------|
| create_main_task | 메인 작업 생성 | - |
| create_sub_task | 서브 작업 생성 | - |
| register_targets | 회수 대상 IP 등록 | - |
| reclaim_dhcp | DHCP 서버 IP 반납 | 10% (Mock) |
| reclaim_device | 장비 IP 삭제 | 5% (Mock) |
| no_action | 작업없음 처리 | - |
| allocate_ip | IP 재할당 (원복) | - |
| complete_sub_task | 서브작업 완료 | - |
| complete_main_task | 메인작업 완료 | - |

---

## 8. Gmail 연동

| 발송 시점 | 수신자 | 내용 |
|-----------|--------|------|
| CONFIRM 처리 시 | 각 IP 담당자 | IP 회수 예정 사전 안내 (IP, NW ID, 팀 정보) |
| DHCP/장비 회수 실패 시 | 관리자 | 오류 유형, IP, 에러 메시지 |

Gmail App Password 방식으로 인증하며, 미설정 시 로그 출력으로 대체됩니다.

---

## 9. 담당자 메일 회신 처리 (Mock)

`POST /scheduler/mail-reply`에 회신 본문(`content`)만 전달하면 **Reclaim Agent가 직접 분석**합니다.

| 회신 내용 | 처리 |
|-----------|------|
| "승인합니다" / "진행해주세요" | APPROVE → IN-PROGRESS 유지 |
| "10.100.1.1 제외해주세요" | REJECT → 해당 IP만 REJECTED |
| "모두 제외해주세요" | REJECT → 전체 IN-PROGRESS REJECTED |

별도 파싱 로직 없이 기존 Reclaim Agent 파이프라인을 그대로 재사용합니다.

---

## 10. 프론트엔드 UI

ChatGPT 스타일의 채팅 인터페이스이며 별도 학습 없이 직관적으로 사용 가능합니다.

**주요 UI 요소:**

- 상단 헤더: 실시간 "팀당 제한: N개 | 추출됨: N건" 표시
- 채팅 영역: 마크다운 표 렌더링 지원 (IP 목록, 진행 현황 표)
- 입력창 왼쪽 클립 아이콘: 엑셀 파일 업로드
- 확정 완료 시: 체크 아이콘과 "작업 등록 완료 (NTOSS 연동됨)" 표시
- 후보 엑셀 추출 시: 검토용 엑셀 다운로드 버튼 자동 노출

---

## 11. 현재 PoC 범위 및 제약사항

| 항목 | PoC | 실제 운영 |
|------|-----|-----------|
| NTOSS 연동 | Mock (가상 응답) | 실제 API 교체 |
| 스케줄러 | API 수동 호출 | 시간 기반 자동 실행 |
| 담당자 메일 회신 | Mock API 호출 | 실제 메일 수신 처리 |
| 인증/권한 | 없음 | 사용자 인증 추가 필요 |
| candidate_agent | 미구현 (별도 개발) | 차수별 후보 추출 자동화 |

---

*본 문서는 2026년 4월 기준 PoC 구현 상태를 기반으로 작성되었습니다.*
