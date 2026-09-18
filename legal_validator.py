from __future__ import annotations

import re
from typing import List


CITATION_RE = re.compile(
    r"(?P<doc>[가-힣A-Za-z0-9·ㆍ()（）\s]{2,60}?(?:법|규정|규칙|지침|기준|예규))"
    r"\s*제\s*(?P<article>\d+)\s*조(?:\s*의\s*(?P<branch>\d+))?",
    flags=re.MULTILINE,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def find_unverified_citations(result_text: str, evidence_text: str, limit: int = 20) -> List[str]:
    evidence_n = _norm(evidence_text)
    warnings: List[str] = []
    seen = set()

    for match in CITATION_RE.finditer(result_text or ""):
        doc = match.group("doc").strip(" -•:：,")
        article = match.group("article")
        branch = match.group("branch")
        citation = f"{doc} 제{article}조" + (f"의{branch}" if branch else "")

        key = _norm(citation)
        if key in seen:
            continue
        seen.add(key)

        doc_n = _norm(doc)
        article_variants = [
            _norm(f"제{article}조"),
            _norm(f"조문번호:{article}"),
            _norm(f"조문번호: {article}"),
        ]
        if branch:
            article_variants.extend(
                [
                    _norm(f"제{article}조의{branch}"),
                    _norm(f"조문번호:{article} 조문가지번호:{branch}"),
                ]
            )

        doc_ok = doc_n in evidence_n
        article_ok = any(v in evidence_n for v in article_variants)

        if not (doc_ok and article_ok):
            warnings.append(citation)
            if len(warnings) >= limit:
                break

    return warnings
