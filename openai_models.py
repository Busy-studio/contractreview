from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from openai import OpenAI


TERRA_MODEL = os.getenv("OPENAI_TERRA_MODEL", "gpt-5.6-terra").strip()
SOL_MODEL = os.getenv("OPENAI_SOL_MODEL", "gpt-5.6-sol").strip()


class OpenAIConfigError(RuntimeError):
    pass


def get_openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise OpenAIConfigError(
            "OPENAI_API_KEY가 설정되지 않았습니다. Streamlit Cloud Secrets 또는 로컬 환경변수에 등록하세요."
        )
    return key


def get_openai_client() -> OpenAI:
    return OpenAI(api_key=get_openai_api_key())


def generate_text(
    prompt: str,
    model: str,
    reasoning_effort: str = "medium",
    max_output_tokens: int = 12000,
) -> str:
    client = get_openai_client()
    response = client.responses.create(
        model=model,
        reasoning={"effort": reasoning_effort},
        input=prompt,
        max_output_tokens=max_output_tokens,
    )
    text = getattr(response, "output_text", None)
    if text:
        return text.strip()
    return str(response)


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = str(text or "").strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    raise ValueError("모델 응답에서 JSON 객체를 찾지 못했습니다.")


def _fallback_intake(contract_text: str) -> Dict[str, Any]:
    text = contract_text.lower()
    issue_tags: List[str] = []

    keyword_map = {
        "지식재산권": ["지식재산", "특허", "발명", "저작권", "성과물"],
        "손해배상": ["손해배상", "배상", "면책", "책임"],
        "비밀유지": ["비밀", "기밀", "nda"],
        "기술이전": ["기술이전", "실시권", "라이선스", "license"],
        "투자": ["투자", "우선주", "전환", "상환", "주주", "신주"],
        "성과공개": ["논문", "공개", "발표", "공표"],
    }

    for tag, keywords in keyword_map.items():
        if any(keyword in text for keyword in keywords):
            issue_tags.append(tag)

    contract_type = "기타/혼합형 계약"
    if any(k in text for k in ["투자계약", "주주간", "우선주", "신주인수"]):
        contract_type = "투자·주주간 계약"
    elif any(k in text for k in ["기술이전", "실시권", "라이선스"]):
        contract_type = "기술이전·실시권 계약"
    elif any(k in text for k in ["공동연구", "공동 연구"]):
        contract_type = "공동연구 계약"
    elif any(k in text for k in ["위탁연구", "용역", "시험", "분석", "자문"]):
        contract_type = "위탁연구·용역 계약"

    queries = [
        f"{contract_type} 지식재산권 연구성과 귀속",
        f"{contract_type} 손해배상 책임 제한",
        "대학 기업 계약 비밀유지 연구성과 공개",
    ]

    return {
        "contract_type": contract_type,
        "issue_tags": issue_tags or ["지식재산권", "손해배상"],
        "law_queries": queries,
        "local_rag_query": " ".join(issue_tags) + " 대학 기업 계약",
        "analysis_note": "Terra 분류 실패 시 규칙 기반 fallback 사용",
    }


def analyze_contract_intake(contract_text: str) -> Dict[str, Any]:
    prompt = f"""
너는 대학 산학협력 계약 검토 시스템의 1차 분류기다.
계약서의 법적 결론을 내리지 말고, 최종 검토에 필요한 검색 범위만 구조화하라.

반드시 JSON 객체 하나만 출력한다.

필드:
- contract_type: 계약 유형. 예) 공동연구, 위탁연구·용역, 기술이전·실시권, NDA, 투자·주주간, 기타/혼합형
- issue_tags: 검토할 핵심 쟁점 3~8개
- law_queries: 국가법령정보센터에서 검색할 한국어 검색어 3~6개. 너무 긴 문장 금지.
- local_rag_query: 대학 내부규정/지침 검색용 검색어
- analysis_note: 계약 유형을 그렇게 분류한 짧은 이유

검색어 작성 원칙:
- 계약서에 실제 나타난 쟁점을 우선
- 지식재산권, 연구성과 귀속, 직무발명, 기술이전, 비밀유지, 손해배상, 연구결과 공개, 투자조건 등 구체적 용어 사용
- 존재하지 않는 법령명이나 조문번호를 추측하지 말 것

[계약서 발췌]
{contract_text[:12000]}
"""

    try:
        raw = generate_text(
            prompt,
            model=TERRA_MODEL,
            reasoning_effort="medium",
            max_output_tokens=2500,
        )
        data = _extract_json_object(raw)
        fallback = _fallback_intake(contract_text)

        data.setdefault("contract_type", fallback["contract_type"])
        data.setdefault("issue_tags", fallback["issue_tags"])
        data.setdefault("law_queries", fallback["law_queries"])
        data.setdefault("local_rag_query", fallback["local_rag_query"])
        data.setdefault("analysis_note", "")

        if not isinstance(data.get("issue_tags"), list):
            data["issue_tags"] = fallback["issue_tags"]
        if not isinstance(data.get("law_queries"), list):
            data["law_queries"] = fallback["law_queries"]

        data["issue_tags"] = [str(x).strip() for x in data["issue_tags"] if str(x).strip()][:8]
        data["law_queries"] = [str(x).strip() for x in data["law_queries"] if str(x).strip()][:6]
        return data
    except Exception as e:
        fallback = _fallback_intake(contract_text)
        fallback["analysis_note"] += f" / {type(e).__name__}: {e}"
        return fallback


def plan_precedent_search(
    contract_type: str,
    issue_tags: List[str],
    primary_evidence: str,
) -> List[str]:
    """
    법령/행정규칙/내부규정만으로 판단 근거가 약한 쟁점에 대해서만
    판례 검색어를 생성한다. 최대 3개.
    """
    prompt = f"""
너는 대학-기업 계약 검토 시스템의 판례 검색 플래너다.
아래 핵심 쟁점과 1차 근거를 비교하여, 법령·행정규칙·내부규정만으로 판단 근거가 충분하지 않고
판례가 실제로 도움이 될 쟁점만 골라 국가법령정보센터 판례검색용 검색어를 생성하라.

반드시 JSON 객체 하나만 출력:
{{
  "queries": ["검색어1", "검색어2"]
}}

원칙:
- 최대 3개
- 판례가 없어도 계약실무상 협상으로 처리할 수 있는 단순 지급조건·문구명확화 문제는 남발하지 않는다.
- 손해배상 예정/책임제한, 해지, 하자, 공동특허, 비밀유지, 사용자책임처럼 법적 해석이 중요한 쟁점을 우선한다.
- 판례 사건번호를 추측하지 않는다.
- 검색어는 8~30자 정도의 한국어 핵심어 조합으로 작성한다.
- 1차 근거가 충분하면 빈 배열을 반환한다.

계약유형: {contract_type}
핵심쟁점: {", ".join(issue_tags)}

[1차 근거]
{primary_evidence[:18000]}
"""
    try:
        raw = generate_text(
            prompt,
            model=TERRA_MODEL,
            reasoning_effort="medium",
            max_output_tokens=1200,
        )
        data = _extract_json_object(raw)
        queries = data.get("queries", [])
        if not isinstance(queries, list):
            return []
        cleaned = []
        seen = set()
        for query in queries:
            q = re.sub(r"\s+", " ", str(query or "")).strip()
            if not q or q in seen:
                continue
            seen.add(q)
            cleaned.append(q[:80])
            if len(cleaned) >= 3:
                break
        return cleaned
    except Exception:
        return []


def generate_final_review(prompt: str) -> str:
    return generate_text(
        prompt,
        model=SOL_MODEL,
        reasoning_effort="high",
        max_output_tokens=18000,
    )
