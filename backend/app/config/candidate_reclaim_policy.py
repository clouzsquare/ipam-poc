"""
IP 회수 후보(엑셀 추출) 선정 정책 — 단일 진입점.

고객이 회수 기준·안내 문구·LLM 프롬프트 가이드를 바꿀 때는 이 파일만 수정하면 됩니다.
(기본 사용률 임계값은 API/Form/.env 의 CANDIDATE_USAGE_THRESHOLD 로 주입)

네트워크명·NTOSS 단지명에 대한 '의미 기반 제외'는 SEMANTIC_EXCLUSION_CATEGORIES 에 항목을
추가하거나 enabled 를 켜는 방식으로 확장합니다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 엑셀 스키마
# ---------------------------------------------------------------------------

RECLAIM_REASON_COLUMN = "회수 사유"

REQUIRED_EXCEL_HEADERS: List[str] = [
    "DHCP Server IP",
    "IP블록",
    "인프라팀",
    "네트워크 이름",
    "네트워크 ID",
    "Primary 여부",
    "사용률(%)",
]

# ---------------------------------------------------------------------------
# 규칙: 사용률
# ---------------------------------------------------------------------------
# True: 사용률 < 임계값일 때만 후보
# False: 사용률 <= 임계값이면 후보 (경계값 포함)
USAGE_STRICTLY_BELOW_THRESHOLD: bool = True

# ---------------------------------------------------------------------------
# 규칙: Primary
# ---------------------------------------------------------------------------
# 엑셀 'Primary 여부' 값이 아래(대소문자 무시, trim)이면 회수 후보에서 제외
PRIMARY_FLAG_EXCLUDES_CANDIDATE: str = "Y"


def usage_qualifies_for_reclaim(usage_percent: float, threshold_percent: float) -> bool:
    if USAGE_STRICTLY_BELOW_THRESHOLD:
        return usage_percent < threshold_percent
    return usage_percent <= threshold_percent


def is_flagged_primary(primary_cell: Any) -> bool:
    """True이면 Primary로 간주되어(후보에서 제외)야 함."""
    if primary_cell is None:
        return False
    return str(primary_cell).strip().upper() == PRIMARY_FLAG_EXCLUDES_CANDIDATE.strip().upper()


def is_non_primary_for_reclaim(primary_cell: Any) -> bool:
    """후보 조건의 'Non-primary' 충족 여부(기존 로직과 동일)."""
    if primary_cell is None:
        return True
    return not is_flagged_primary(primary_cell)


# ---------------------------------------------------------------------------
# 의미 기반 제외 (네트워크명 / NTOSS 단지명 — LLM) — 카테고리 확장
# ---------------------------------------------------------------------------
# id: 고정 식별자 (MATCHES 줄에 그대로 사용)
# title: 사람에게 보고할 라벨
# guidance: 분류 시 참고할 설명
# examples: 예시 명칭 (선택)
# enabled: False 이면 프롬프트에 포함하지 않음 (추가 후 켜기 용)

SEMANTIC_EXCLUSION_CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "short_term_lodging",
        "title": "단기 숙박·숙소형 시설",
        "guidance": (
            "특정 요일·시점에 사용량이 급증·급감할 수 있는 단기 숙박 시설에 해당하는지. "
            "일반 주거 아파트·일반 상업시설은 해당 없음."
        ),
        "examples": ["기숙사", "호텔", "숙박업소", "모텔", "리조트", "게스트하우스"],
        "enabled": True,
    },
    {
        "id": "phone_retail",
        "title": "휴대폰·통신 대리점",
        "guidance": (
            "휴대폰 판매·대리점, 통신사 직영·대리점 등 단기 이동·이벤트 매장 특성이 강한 명칭인지."
        ),
        "examples": ["OO텔레콤", "휴대폰 대리점", "공식대리점"],
        "enabled": False,
    },
]


def active_semantic_exclusion_categories() -> List[Dict[str, Any]]:
    return [c for c in SEMANTIC_EXCLUSION_CATEGORIES if c.get("enabled", True)]


def category_title_by_id() -> Dict[str, str]:
    return {str(c["id"]): str(c["title"]) for c in SEMANTIC_EXCLUSION_CATEGORIES if "id" in c}


def format_semantic_match_labels(match_ids: Tuple[str, ...]) -> str:
    """MATCHES에 나온 id를 사람이 읽는 제목으로."""
    m = category_title_by_id()
    if not match_ids:
        return ""
    return ", ".join(m.get(i, i) for i in match_ids)


# ---------------------------------------------------------------------------
# 사람에게 보이는 ‘선정 기준’ 한 줄 (채팅 요약, LLM 프롬프트, 폴백 공통)
# ---------------------------------------------------------------------------


def get_selection_criteria_summary() -> str:
    """선정 기준 한 줄 — 활성화된 의미 기반 제외 유형을 반영."""
    sem = active_semantic_exclusion_categories()
    if not sem:
        return "사용률 미달 + Non-primary"
    labels = "·".join(str(c["title"]) for c in sem)
    return f"사용률 미달 + Non-primary + ({labels} 등 명칭 기반 제외)"


# 하위 호환: 모듈 로드 시점 기준 문자열
SELECTION_CRITERIA_SUMMARY = get_selection_criteria_summary()

# ---------------------------------------------------------------------------
# 제외 사유 문구 (제외 목록 / exclude_reason)
# ---------------------------------------------------------------------------

MSG_EXCLUDE_USAGE = "사용률 {usage:.2f}%가 기준({threshold:.2f}%) 미만이 아님"
MSG_EXCLUDE_PRIMARY = f"Primary 여부가 {PRIMARY_FLAG_EXCLUDES_CANDIDATE}이므로 제외"
MSG_EXCLUDE_SEMANTIC = "네트워크명 또는 NTOSS 아파트명이 의미 기반 제외로 분류됨 ({detail})"
MSG_EXCLUDE_DUPLICATE = "엑셀 내 중복 대상"
MSG_EXCLUDE_FALLBACK = "정책 기준 미충족"


def exclude_reason_parts(
    *,
    qualifies_usage: bool,
    passes_non_primary: bool,
    excluded_by_semantic: bool,
    semantic_detail: str,
    usage_percent: float,
    threshold_percent: float,
) -> List[str]:
    """선정 실패 시 제외 사유 문자열 목록(순서: 사용률 → Primary → 의미 기반)."""
    reasons: List[str] = []
    if not qualifies_usage:
        reasons.append(MSG_EXCLUDE_USAGE.format(usage=usage_percent, threshold=threshold_percent))
    if not passes_non_primary:
        reasons.append(MSG_EXCLUDE_PRIMARY)
    if excluded_by_semantic:
        reasons.append(MSG_EXCLUDE_SEMANTIC.format(detail=semantic_detail or "유형 미상"))
    return reasons


# ---------------------------------------------------------------------------
# LLM: 의미 기반 제외 분류 — {name} 치환
# ---------------------------------------------------------------------------


def _build_semantic_category_block() -> str:
    lines: List[str] = []
    for c in active_semantic_exclusion_categories():
        lines.append(f"- id: {c['id']}")
        lines.append(f"  제목: {c['title']}")
        lines.append(f"  판단: {c['guidance']}")
        ex = c.get("examples") or []
        if ex:
            lines.append(f"  예시 명칭: {', '.join(str(x) for x in ex)}")
    return "\n".join(lines) if lines else "(활성화된 제외 유형 없음)"


SEMANTIC_EXCLUSION_PROMPT_TEMPLATE = """\
당신은 IPAM 회수 후보 선정을 위한 명칭 분류기입니다.

[아래 유형 중 하나라도 해당하면 EXCLUDE, 아니면 KEEP]
{_category_block}

명칭: "{name}"

반드시 아래 두 줄만 출력하세요 (다른 줄 없음).
VERDICT: EXCLUDE 또는 KEEP
MATCHES: 해당하는 유형의 id를 쉼표로 나열. 해당 없으면 NONE
"""


def build_semantic_exclusion_prompt(name: str) -> str:
    safe = (name or "").replace("{", "{{").replace("}", "}}")
    block = _build_semantic_category_block()
    return SEMANTIC_EXCLUSION_PROMPT_TEMPLATE.replace("{_category_block}", block).replace("{name}", safe)


def parse_semantic_exclusion_response(content: str) -> Tuple[bool, Tuple[str, ...]]:
    """
    LLM 응답에서 (제외 여부, 매칭된 카테고리 id 튜플) 파싱.
    VERDICT 줄이 없으면 첫 토큰이 EXCLUDE/KEEP 인 구형 한 줄 응답도 허용.
    """
    text = str(content or "").strip()
    if not text:
        return False, ()
    excluded = False
    matches: List[str] = []
    verdict_found = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        u = line.upper()
        if u.startswith("VERDICT:"):
            verdict_found = True
            v = line.split(":", 1)[1].strip().upper()
            excluded = v == "EXCLUDE"
        elif u.startswith("MATCHES:"):
            m = line.split(":", 1)[1].strip()
            if m.upper() != "NONE" and m:
                matches = [x.strip() for x in m.split(",") if x.strip()]
    if not verdict_found:
        lines_list = [x.strip() for x in text.splitlines() if x.strip()]
        if lines_list:
            fl = lines_list[0].upper()
            if fl == "EXCLUDE" or fl.startswith("EXCLUDE"):
                excluded = True
            elif fl == "KEEP" or fl.startswith("KEEP"):
                excluded = False
            else:
                excluded = "EXCLUDE" in text.upper()
    return excluded, tuple(matches)


# ---------------------------------------------------------------------------
# LLM: 회수 사유 한 줄 생성 시 ‘판정 기준’ 설명 (프롬프트에 삽입)
# ---------------------------------------------------------------------------

LLM_DECISION_CRITERIA_DESCRIPTION = (
    "사용률 임계치, Primary 여부, 네트워크명·단지명 의미 기반 제외"
)


def build_decision_reason_llm_prompt(row_context: dict, excluded: bool) -> str:
    return f"""
아래 판정 결과를 관리자에게 설명할 한 줄 사유를 작성하세요.
- excluded={excluded}
- 판정 기준: {LLM_DECISION_CRITERIA_DESCRIPTION}
- 데이터: {row_context}
40자 이내 한국어 문장으로 출력하세요.
"""


# ---------------------------------------------------------------------------
# LLM: 엑셀 분석 결과 채팅 응답 (선정 기준 문구는 get_selection_criteria_summary)
# ---------------------------------------------------------------------------

EXTRACT_RESPONSE_TEMPLATE_FOOTER = (
    "후보 확인 후 '메일 발송'이라고 입력하면 검토 메일을 인프라 담당자에게 발송하고, "
    "수정이 필요하다면 수정할 내용을 입력해주세요."
)


def build_extract_analysis_llm_prompt(result_dict: dict) -> str:
    crit = get_selection_criteria_summary()
    footer = EXTRACT_RESPONSE_TEMPLATE_FOOTER
    return f"""
당신은 IPAM AI Assistant입니다.
아래 데이터를 바탕으로 "정해진 양식"으로만 응답하세요.

[중요 규칙]
1) 후보 목록(selected_preview)과 제외 목록(excluded_details)을 절대 요약/생략하지 말고 전부 출력하세요.
2) 기준 IP사용률(usage_threshold_percent)을 반드시 명시하세요.
3) 제외 목록은 각 항목의 exclude_reason을 그대로 포함하세요.
4) 데이터에 없는 내용을 임의로 만들지 마세요.
5) 아래 출력 템플릿의 제목/순서를 그대로 지키세요.
6) 마지막 안내 문장은 반드시 아래 문장과 100% 동일해야 합니다.
   {footer}

[출력 템플릿]
엑셀 분석 결과 요약
- 후보 건수: {{selected_count}}건
- 제외 건수: {{skipped_count}}건
- 기준 IP사용률: {{usage_threshold_percent}}%
- 선정 기준: {crit}

후보 목록
- {{owner_team}} | {{nw_id}} | {{ip_address}} | 사용률 {{usage_percent}}% | 근거: {{decision_reason}}
- ... (selected_preview의 모든 항목)

제외 목록
- {{owner_team}} | {{nw_id}} | {{ip_address}} | 사용률 {{usage_percent}}% | 제외 사유: {{exclude_reason}}
- ... (excluded_details의 모든 항목)

{footer}

[입력 데이터]
{result_dict}
"""


def selection_policy_payload(usage_threshold: float) -> dict:
    """API 결과 selection_policy 필드 — 프론트/연동용 요약."""
    cats = active_semantic_exclusion_categories()
    return {
        "usage_threshold_percent": usage_threshold,
        "non_primary_required": True,
        "exclude_semantic_categories": [c["id"] for c in cats],
        "criteria_summary": get_selection_criteria_summary(),
        "usage_rule": "strictly_below" if USAGE_STRICTLY_BELOW_THRESHOLD else "below_or_equal",
        "primary_exclude_flag": PRIMARY_FLAG_EXCLUDES_CANDIDATE,
        # 하위 호환
        "exclude_accommodation": any(c.get("id") == "short_term_lodging" for c in cats),
    }
