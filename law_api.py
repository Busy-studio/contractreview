from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional


LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"


class OpenLawAPIError(RuntimeError):
    pass


def get_open_law_api_key() -> str:
    return (
        os.getenv("OPEN_LAW_API_KEY", "").strip()
        or os.getenv("LAW_API_KEY", "").strip()
        or os.getenv("LAW_OPEN_API_KEY", "").strip()
    )


def open_law_enabled() -> bool:
    return bool(get_open_law_api_key())


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _normalize_query(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:180]


def _first_value(record: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return None


def _find_records(obj: Any, id_keys: Iterable[str]) -> List[Dict[str, Any]]:
    id_keys = tuple(id_keys)
    found: List[Dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if any(k in value and value.get(k) not in (None, "") for k in id_keys):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for record in found:
        ident = _first_value(record, id_keys)
        if not ident:
            continue
        key = tuple(sorted((str(k), str(v)) for k, v in record.items() if _scalar(v)))
        if (ident, key) in seen:
            continue
        seen.add((ident, key))
        deduped.append(record)
    return deduped


def _flatten_json(obj: Any, max_chars: int = 16000) -> str:
    lines: List[str] = []

    def walk(value: Any, prefix: str = "") -> None:
        if sum(len(x) + 1 for x in lines) >= max_chars:
            return

        if isinstance(value, dict):
            for key, child in value.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                walk(child, next_prefix)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{prefix}[{idx}]")
        elif _scalar(value):
            text = "" if value is None else str(value).strip()
            if not text:
                return
            key = prefix.split(".")[-1] if prefix else "값"
            lines.append(f"{key}: {text}")

    walk(obj)
    text = "\n".join(lines)
    return text[:max_chars]


SOURCE_CONFIG = {
    "law": {
        "label": "현행법령",
        "target": "eflaw",
        "id_keys": ("법령ID", "법령일련번호"),
        "name_keys": ("법령명한글", "법령명", "법령약칭명"),
        "date_keys": ("시행일자", "공포일자"),
    },
    "admrul": {
        "label": "행정규칙",
        "target": "admrul",
        "id_keys": ("행정규칙일련번호", "행정규칙ID"),
        "name_keys": ("행정규칙명",),
        "date_keys": ("발령일자",),
    },
    "prec": {
        "label": "판례",
        "target": "prec",
        "id_keys": ("판례일련번호", "판례정보일련번호"),
        "name_keys": ("사건명", "판례명"),
        "date_keys": ("선고일자",),
    },
    "expc": {
        "label": "법령해석례",
        "target": "expc",
        "id_keys": ("법령해석례일련번호",),
        "name_keys": ("안건명", "법령해석례명"),
        "date_keys": ("해석일자",),
    },
}


class OpenLawClient:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 12):
        self.api_key = (api_key or get_open_law_api_key()).strip()
        if not self.api_key:
            raise OpenLawAPIError(
                "OPEN_LAW_API_KEY가 설정되지 않았습니다. 국가법령정보 공동활용 API 인증값을 Secrets에 등록하세요."
            )
        self.timeout = timeout
        self._cache: Dict[str, Any] = {}

    def _get_json(self, endpoint: str, params: Dict[str, Any]) -> Any:
        clean = {"OC": self.api_key, "type": "JSON"}
        clean.update({k: v for k, v in params.items() if v not in (None, "")})
        query = urllib.parse.urlencode(clean, doseq=True)
        url = f"{endpoint}?{query}"

        if url in self._cache:
            return self._cache[url]

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "PNU-ContractReview/1.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise OpenLawAPIError(f"국가법령정보 API HTTP 오류: {e.code}") from e
        except urllib.error.URLError as e:
            raise OpenLawAPIError(f"국가법령정보 API 연결 오류: {e.reason}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            snippet = re.sub(r"\s+", " ", raw[:300])
            raise OpenLawAPIError(f"국가법령정보 API JSON 해석 실패: {snippet}") from e

        self._cache[url] = data
        return data

    def search(self, source: str, query: str, display: int = 3) -> List[Dict[str, Any]]:
        if source not in SOURCE_CONFIG:
            raise ValueError(f"지원하지 않는 법령정보 소스입니다: {source}")

        cfg = SOURCE_CONFIG[source]
        params: Dict[str, Any] = {
            "target": cfg["target"],
            "query": _normalize_query(query),
            "display": max(1, min(int(display), 20)),
            "page": 1,
            "search": 2,
        }
        if source == "law":
            params["nw"] = 3
        elif source == "admrul":
            params["nw"] = 1

        payload = self._get_json(LAW_SEARCH_URL, params)
        records = _find_records(payload, cfg["id_keys"])

        results: List[Dict[str, Any]] = []
        for record in records:
            ident = _first_value(record, cfg["id_keys"])
            name = _first_value(record, cfg["name_keys"]) or "(명칭 없음)"
            date = _first_value(record, cfg["date_keys"])
            if not ident:
                continue
            results.append(
                {
                    "source": source,
                    "label": cfg["label"],
                    "id": ident,
                    "name": name,
                    "date": date,
                    "record": record,
                }
            )
        return results[: max(1, min(display, 20))]

    def detail(self, item: Dict[str, Any]) -> Any:
        source = item["source"]
        record = item.get("record") or {}
        cfg = SOURCE_CONFIG[source]

        params: Dict[str, Any] = {"target": cfg["target"]}

        if source == "law":
            law_id = _first_value(record, ("법령ID",))
            mst = _first_value(record, ("법령일련번호",))
            if law_id:
                params["ID"] = law_id
            elif mst:
                params["MST"] = mst
            else:
                raise OpenLawAPIError("현행법령 상세조회에 필요한 법령ID/일련번호가 없습니다.")
        elif source == "admrul":
            ident = _first_value(record, ("행정규칙일련번호", "행정규칙ID")) or item.get("id")
            params["ID"] = ident
        elif source == "prec":
            ident = _first_value(record, ("판례일련번호", "판례정보일련번호")) or item.get("id")
            params["ID"] = ident
        elif source == "expc":
            ident = _first_value(record, ("법령해석례일련번호",)) or item.get("id")
            params["ID"] = ident

        return self._get_json(LAW_SERVICE_URL, params)


def collect_legal_evidence(
    queries: Iterable[str],
    max_items: int = 8,
    max_queries: int = 4,
) -> Dict[str, Any]:
    if not open_law_enabled():
        return {
            "enabled": False,
            "evidence": "",
            "items": [],
            "warning": "OPEN_LAW_API_KEY 미설정으로 국가법령정보 API 조회를 건너뜁니다.",
        }

    cleaned_queries: List[str] = []
    seen_queries = set()
    for query in queries:
        q = _normalize_query(query)
        if not q or q in seen_queries:
            continue
        seen_queries.add(q)
        cleaned_queries.append(q)
        if len(cleaned_queries) >= max_queries:
            break

    if not cleaned_queries:
        cleaned_queries = ["대학 기업 연구계약 지식재산권 손해배상"]

    client = OpenLawClient()
    items: List[Dict[str, Any]] = []
    seen_items = set()
    warnings: List[str] = []

    for query in cleaned_queries:
        if len(items) >= max_items:
            break

        for source in ("law", "admrul", "prec", "expc"):
            if len(items) >= max_items:
                break

            try:
                candidates = client.search(source, query, display=2)
            except Exception as e:
                warnings.append(f"{SOURCE_CONFIG[source]['label']} 검색 실패: {e}")
                continue

            for candidate in candidates:
                dedupe_key = (candidate["source"], candidate["id"])
                if dedupe_key in seen_items:
                    continue

                try:
                    detail = client.detail(candidate)
                    detail_text = _flatten_json(detail, max_chars=12000)
                except Exception as e:
                    warnings.append(
                        f"{candidate['label']} 상세조회 실패({candidate['name']}): {e}"
                    )
                    continue

                seen_items.add(dedupe_key)
                candidate = dict(candidate)
                candidate.pop("record", None)
                candidate["query"] = query
                candidate["detail_text"] = detail_text
                items.append(candidate)

                # 한 검색어에서 동일 소스는 가장 관련도 높은 1건만 상세조회
                break

    evidence_blocks: List[str] = []
    for idx, item in enumerate(items, start=1):
        header = (
            f"[공식 근거 {idx} | {item['label']}] {item['name']}"
            + (f" | 기준일자: {item['date']}" if item.get("date") else "")
            + f" | 검색어: {item.get('query', '')}"
        )
        evidence_blocks.append(
            header
            + "\n출처: 국가법령정보센터 Open API"
            + "\n"
            + item.get("detail_text", "")
        )

    return {
        "enabled": True,
        "evidence": "\n\n".join(evidence_blocks),
        "items": items,
        "warning": "\n".join(warnings[:12]),
    }
