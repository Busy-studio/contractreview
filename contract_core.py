import hashlib
import math
import os
import pickle
import re
import shutil
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import List

import fitz
import gdown
from docx import Document
from law_api import collect_legal_evidence
from legal_validator import find_unverified_citations
from openai_models import (
    SOL_MODEL,
    TERRA_MODEL,
    analyze_contract_intake,
    generate_final_review,
)

DEFAULT_LAW_ZIP_LINK = "https://drive.google.com/file/d/1Wu5sEWPwdH7AX2n_08ViNCEZtZnhsMwH/view?usp=sharing"
CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class ConfigError(RuntimeError):
    pass


class ValidationError(ValueError):
    pass


def download_drive_file(url: str, output_path: str) -> str:
    if not url or "/d/" not in url:
        raise ValidationError("올바른 Google Drive 파일 링크 형식이 아닙니다.")

    file_id = url.split("/d/")[1].split("/")[0]
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists():
        output_file.unlink()

    result = gdown.download(id=file_id, output=str(output_file), quiet=True)
    if not result or not output_file.exists():
        raise ValidationError("법령 zip 다운로드에 실패했습니다. 공유 설정을 확인하세요.")

    if not zipfile.is_zipfile(output_file):
        raise ValidationError("다운로드한 파일이 유효한 zip 형식이 아닙니다.")

    return str(output_file)


def validate_zip_path(zip_path: str) -> str:
    if not zip_path or not str(zip_path).strip():
        raise ValidationError("법령/규정 zip 파일 경로가 비어 있습니다.")

    zip_path = str(zip_path).strip()
    if not os.path.exists(zip_path):
        raise ValidationError(f"zip 파일을 찾을 수 없습니다: {zip_path}")

    if not zipfile.is_zipfile(zip_path):
        raise ValidationError("업로드한 파일이 유효한 zip 형식이 아닙니다.")

    return zip_path


def resolve_law_zip(zip_path: str | None = None, zip_link: str | None = None) -> str:
    if zip_path and str(zip_path).strip():
        return validate_zip_path(zip_path)

    # 사용자가 외부 링크를 명시한 경우 저장소 기본 ZIP보다 우선한다.
    if zip_link and str(zip_link).strip():
        link = str(zip_link).strip()
        hashed = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
        cached_zip_path = CACHE_DIR / f"law_zip_{hashed}.zip"
        if cached_zip_path.exists() and zipfile.is_zipfile(cached_zip_path):
            return str(cached_zip_path)
        return download_drive_file(link, str(cached_zip_path))

    local_zip_path = Path("lawcollect.zip")
    if local_zip_path.exists() and zipfile.is_zipfile(local_zip_path):
        return str(local_zip_path)

    link = DEFAULT_LAW_ZIP_LINK.strip()
    hashed = hashlib.md5(link.encode("utf-8")).hexdigest()[:16]
    cached_zip_path = CACHE_DIR / f"law_zip_{hashed}.zip"

    if cached_zip_path.exists() and zipfile.is_zipfile(cached_zip_path):
        return str(cached_zip_path)

    return download_drive_file(link, str(cached_zip_path))


def extract_pdf(path: str) -> str:
    doc = fitz.open(path)
    texts = []
    try:
        for i, page in enumerate(doc, start=1):
            t = page.get_text("text")
            if t and t.strip():
                texts.append(f"[페이지 {i}]\n{t}")
    finally:
        doc.close()
    return "\n\n".join(texts)


def extract_docx(path: str) -> str:
    doc = Document(path)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])


def extract_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".docx":
        return extract_docx(path)
    if ext in [".txt", ".md"]:
        return extract_txt(path)
    raise ValidationError(f"지원하지 않는 파일 형식입니다: {ext}")


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int = 1200, overlap: int = 200) -> List[str]:
    text = clean_text(text)
    if not text:
        return []

    chunks = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + size, n)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start += size - overlap

    return chunks


def strip_korean_particles(s: str) -> str:
    s = s.strip()
    particles = [
        "으로부터", "에게서", "까지는", "까지도", "에게는", "에서는", "으로는",
        "으로서", "으로써", "이라도", "라고", "라고는", "라도", "까지", "부터",
        "에게", "에서", "으로", "와의", "과의", "와", "과", "은", "는", "이", "가",
        "을", "를", "의", "에", "도", "만"
    ]

    changed = True
    while changed:
        changed = False
        for p in sorted(particles, key=len, reverse=True):
            if s.endswith(p) and len(s) > len(p) + 1:
                s = s[:-len(p)].strip()
                changed = True
                break

    return s


def normalize_token(s: str) -> str:
    s = str(s).strip()
    s = s.strip("'\"“”‘’")
    s = re.sub(r"\s+", " ", s)
    s = s.strip()
    s = strip_korean_particles(s)
    return s.strip()


def is_generic_noun(token: str) -> bool:
    token = normalize_token(token)
    generic_block = {
        "상대방", "당사자", "양당사자", "제3자", "직원", "자료", "정보", "결과물",
        "연구결과", "연구개발", "지식재산권", "손해배상", "비밀보장", "표시권", "공표권",
        "동일성", "사전", "요청", "내용", "방법", "범위", "기간", "비용", "보고서",
        "실험데이터", "설계도", "소스코드", "기술", "권리", "의무", "연구", "계약"
    }
    return token in generic_block


def is_valid_alias_token(token: str) -> bool:
    token = normalize_token(token)
    if not token:
        return False
    if len(token) < 2 or len(token) > 40:
        return False
    if is_generic_noun(token):
        return False

    bad_suffixes = (
        "한다", "하여", "하며", "되고", "되는", "대한", "관한", "위한", "통한",
        "있음", "없음", "경우", "사항", "내용", "방법", "비용", "사전", "요청"
    )
    if token.endswith(bad_suffixes):
        return False
    return True


def anonymize_contract_text(text: str):
    mapping = {}
    reverse_mapping = {}
    counters = defaultdict(int)

    def next_label(label_type):
        counters[label_type] += 1
        return f"[{label_type}_{counters[label_type]}]"

    def add_mapping(token: str, label_type: str, forced_label: str = None):
        token = normalize_token(token)
        if not token:
            return None
        if token in mapping:
            return mapping[token]
        label = forced_label if forced_label else next_label(label_type)
        mapping[token] = label
        reverse_mapping[label] = token
        return label

    def replace_token_everywhere(text: str, token: str, label: str):
        token = normalize_token(token)
        if not token:
            return text
        variants = [token, f"'{token}'", f'"{token}"', f"‘{token}’", f"“{token}”"]
        for v in sorted(set(variants), key=len, reverse=True):
            text = text.replace(v, label)
        escaped = re.escape(token)
        text = re.sub(rf'(?<![\w가-힣]){escaped}(?![\w가-힣])', label, text)
        return text

    def guess_label_type(entity: str = "", alias: str = ""):
        entity_n = normalize_token(entity)
        alias_n = normalize_token(alias)
        party_aliases = {
            "갑", "을", "병", "정", "무", "기",
            "연구기관", "발주자", "수행기관", "위탁기관", "수탁기관",
            "공급자", "수요자", "계약상대방"
        }
        if alias_n in party_aliases:
            return "당사자"
        if any(k in entity_n for k in ["주식회사", "㈜", "유한회사", "회사", "Inc", "Ltd", "Corp"]):
            return "기업명"
        if any(k in entity_n for k in ["대학교", "산학협력단", "연구원", "재단", "센터", "학교", "대학"]):
            return "기관명"
        if alias_n in {"비밀정보", "제한적 오픈소스 코드"}:
            return "정의용어"
        return "정의용어"

    base_patterns = [
        (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', "이메일"),
        (r'01\d-\d{3,4}-\d{4}', "휴대전화"),
        (r'0\d{1,2}-\d{3,4}-\d{4}', "전화번호"),
        (r'\d{3}-\d{2}-\d{5}', "사업자등록번호"),
        (r'\d{1,3}(,\d{3})+(?:\.\d+)?원|\d+원', "금액"),
        (r'20\d{2}[.\-/년 ]\s?\d{1,2}[.\-/월 ]\s?\d{1,2}(?:일)?', "날짜"),
        (r'(?:계약번호|관리번호|문서번호)\s*[:：]?\s*[A-Za-z0-9\-_/]+', "문서번호"),
        (r'(?:특허번호|출원번호|등록번호)\s*[:：]?\s*[A-Za-z0-9\-]+', "지식재산번호"),
        (r'(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)[^\n,]{0,50}(?:로|길|동|읍|면|리)\s*\d+[^\n,]{0,20}', "주소"),
    ]

    for pattern, label_type in base_patterns:
        found = re.findall(pattern, text)
        values = []
        for f in found:
            if isinstance(f, tuple):
                values.append(normalize_token(max(f, key=len)))
            else:
                values.append(normalize_token(f))

        for token in sorted(set([v for v in values if v]), key=len, reverse=True):
            label = add_mapping(token, label_type)
            text = replace_token_everywhere(text, token, label)

    alias_patterns = [
        r'(?P<entity>[^\n()]{2,180}?)\s*\(\s*이하\s*[\'\"“”‘’](?P<alias>[^\'\"“”‘’]{1,50})[\'\"“”‘’]\s*이라?\s*한다\s*\)',
        r'(?P<entity>[^\n()]{2,180}?)\s*\(\s*이하\s*[\'\"“”‘’](?P<alias>[^\'\"“”‘’]{1,50})[\'\"“”‘’]\s*\)',
        r'(?P<entity>[^\n()]{2,180}?)\s*이하\s*[\'\"“”‘’](?P<alias>[^\'\"“”‘’]{1,50})[\'\"“”‘’]\s*이라?\s*한다',
    ]

    alias_pairs = []
    for pat in alias_patterns:
        for m in re.finditer(pat, text):
            entity = normalize_token(m.group("entity"))
            alias = normalize_token(m.group("alias"))
            if not alias:
                continue
            entity = re.sub(r'^(본 계약과 관련하여|계약 수행 중|계약 수행을 위해|본 연구개발의 결과로 발생하는|계약 수행 과정에서)\s*', '', entity).strip()
            entity = re.sub(r'\s*(은|는|이|가|을|를|에|에게|으로부터|와|과)$', '', entity).strip()
            if not is_valid_alias_token(alias):
                continue
            alias_pairs.append((entity, alias))

    for entity, alias in sorted(alias_pairs, key=lambda x: len(x[0]) + len(x[1]), reverse=True):
        label_type = guess_label_type(entity, alias)
        if entity and not is_generic_noun(entity):
            label = add_mapping(entity, label_type)
            add_mapping(alias, label_type, forced_label=label)
            text = replace_token_everywhere(text, entity, label)
            text = replace_token_everywhere(text, alias, label)
        else:
            label = add_mapping(alias, label_type)
            text = replace_token_everywhere(text, alias, label)

    quoted_tokens = re.findall(r"[\"'“‘]([^\"'”’\n]{2,50})[\"'”’]", text)
    freq = defaultdict(int)
    for q in quoted_tokens:
        qn = normalize_token(q)
        if not qn:
            continue
        if qn.startswith("[") and qn.endswith("]"):
            continue
        if not is_valid_alias_token(qn):
            continue
        freq[qn] += 1

    repeated_quoted = [q for q, cnt in freq.items() if cnt >= 2]
    for token in sorted(set(repeated_quoted), key=len, reverse=True):
        if token in mapping:
            label = mapping[token]
        else:
            label_type = "정의용어"
            if token in {"연구기관", "발주자", "수행기관", "위탁기관", "수탁기관", "갑", "을", "병", "정", "무", "기"}:
                label_type = "당사자"
            label = add_mapping(token, label_type)
        text = replace_token_everywhere(text, token, label)

    org_patterns = [
        r'(주식회사\s*[가-힣A-Za-z0-9&·\-. ]{1,40})',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}대학교\s*산학협력단)',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}대학교)',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}연구원)',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}재단)',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}센터)',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}주식회사)',
        r'([가-힣A-Za-z0-9&·\-. ]{2,40}㈜)',
    ]

    org_candidates = []
    for pat in org_patterns:
        for m in re.findall(pat, text):
            val = normalize_token(m if not isinstance(m, tuple) else max(m, key=len))
            if not val:
                continue
            if is_generic_noun(val):
                continue
            org_candidates.append(val)

    for org in sorted(set(org_candidates), key=len, reverse=True):
        if org in mapping:
            continue
        label_type = guess_label_type(org)
        if label_type not in {"기업명", "기관명"}:
            continue
        label = add_mapping(org, label_type)
        text = replace_token_everywhere(text, org, label)

    person_patterns = [
        r'(?:연구책임자|책임연구원|담당자|대표자|성명|이름)\s*[:：]?\s*([가-힣]{2,4})',
        r'([가-힣]{2,4})\s*(?:교수|박사|대표이사|책임연구원)',
    ]

    person_candidates = []
    for pat in person_patterns:
        for m in re.findall(pat, text):
            val = normalize_token(m if not isinstance(m, tuple) else max(m, key=len))
            if 2 <= len(val) <= 4:
                person_candidates.append(val)

    blocked_person = {"표시권", "공표권", "동일성"}
    for name in sorted(set(person_candidates), key=len, reverse=True):
        if name in blocked_person or name in mapping:
            continue
        label = add_mapping(name, "담당자")
        text = replace_token_everywhere(text, name, label)

    return text, mapping, reverse_mapping


def post_fix_alias_leaks(text, mapping):
    for token, label in sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True):
        token_n = token.strip()
        variants = [token_n, f"'{token_n}'", f'"{token_n}"', f"‘{token_n}’", f"“{token_n}”"]
        for v in variants:
            text = text.replace(v, label)
    text = re.sub(r"[\"'“‘](\[[^\]]+\])[\"'”’]", r"\1", text)
    return text


def deanonymize_text(text: str, reverse_mapping: dict):
    if not reverse_mapping:
        return text
    for label, original in sorted(reverse_mapping.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(label, original)
    return text


def find_possible_leaks(text):
    leak_patterns = [
        r"[‘“'\"]?[가-힣A-Za-z0-9&·\-.]{2,30}[’”'\"]?\s*이라 한다",
        r"[‘“'\"]?[가-힣A-Za-z0-9&·\-.]{2,30}[’”'\"]?\s*의 요청",
        r"[‘“'\"]?[가-힣A-Za-z0-9&·\-.]{2,30}[’”'\"]?\s*의 사전",
    ]
    found = set()
    for pat in leak_patterns:
        for m in re.findall(pat, text):
            if isinstance(m, tuple):
                m = max(m, key=len)
            if "[" not in str(m):
                found.add(str(m).strip())
    return sorted(found)


def tokenize_kr(text: str) -> List[str]:
    text = clean_text(text).lower()
    return re.findall(r"[가-힣a-zA-Z0-9]+", text)


def _zip_fingerprint(zip_path: str) -> str:
    # 파일 경로/mtime이 아니라 ZIP 실제 내용으로 식별한다.
    # 규정 ZIP이 바뀌면 자동으로 새 인덱스를 만들고, 내용이 같으면 기존 캐시를 재사용한다.
    digest = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_sparse_index(chunks: List[str], meta: List[str]):
    doc_tokens = [tokenize_kr(c) for c in chunks]
    doc_freq = defaultdict(int)

    for tokens in doc_tokens:
        for term in set(tokens):
            doc_freq[term] += 1

    avg_doc_len = sum(len(tokens) for tokens in doc_tokens) / max(len(doc_tokens), 1)

    return {
        "chunks": chunks,
        "meta": meta,
        "doc_tokens": doc_tokens,
        "doc_freq": dict(doc_freq),
        "num_docs": len(doc_tokens),
        "avg_doc_len": avg_doc_len,
    }


def build_index(zip_path: str):
    zip_path = validate_zip_path(zip_path)
    fp = _zip_fingerprint(zip_path)
    cache_file = CACHE_DIR / f"index_{fp}.pkl"
    unzip_dir = CACHE_DIR / f"unzipped_{fp}"

    if cache_file.exists():
        with open(cache_file, "rb") as f:
            return pickle.load(f)

    if unzip_dir.exists():
        shutil.rmtree(unzip_dir)
    unzip_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(unzip_dir)

    files = []
    for root, _, fs in os.walk(unzip_dir):
        for name in fs:
            if name.lower().endswith((".pdf", ".docx", ".txt", ".md")):
                files.append(os.path.join(root, name))

    if not files:
        raise ValidationError("zip 내부에서 읽을 수 있는 PDF/DOCX/TXT/MD 파일을 찾지 못했습니다.")

    chunks = []
    meta = []
    for f in files:
        try:
            text = extract_text(f)
            cs = chunk_text(text, size=1200, overlap=200)
            for c in cs:
                chunks.append(c)
                meta.append(f)
        except Exception as e:
            print(f"[경고] 파일 처리 실패: {f} / {e}")

    if not chunks:
        raise ValidationError("법령/규정 zip에서 텍스트를 추출하지 못했습니다.")

    index = build_sparse_index(chunks, meta)

    with open(cache_file, "wb") as f:
        pickle.dump(index, f)

    return index


def load_internal_index(zip_path: str | None = None, zip_link: str | None = None):
    """
    대학 내부규정/축적 문서 인덱스를 로드한다.

    - ZIP이 있으면 ZIP 내용 해시 기반 캐시를 사용한다.
    - ZIP이 교체되면 자동으로 새 인덱스를 생성한다.
    - ZIP을 찾을 수 없는 구버전 배포 환경에서만 기존 law_index.pkl을 fallback으로 사용한다.
    """
    try:
        resolved_zip_path = resolve_law_zip(zip_path=zip_path, zip_link=zip_link)
        return build_index(resolved_zip_path)
    except Exception:
        legacy_index = Path("law_index.pkl")
        if legacy_index.exists():
            with open(legacy_index, "rb") as f:
                return pickle.load(f)
        raise


def bm25_score(query_tokens, doc_tokens, doc_freq, num_docs, avg_doc_len, k1=1.5, b=0.75):
    score = 0.0
    doc_len = len(doc_tokens)
    tf = Counter(doc_tokens)

    for term in query_tokens:
        if term not in tf:
            continue

        df = doc_freq.get(term, 0)
        if df == 0:
            continue

        idf = math.log((num_docs - df + 0.5) / (df + 0.5) + 1)
        freq = tf[term]
        denom = freq + k1 * (1 - b + b * (doc_len / max(avg_doc_len, 1e-9)))
        score += idf * ((freq * (k1 + 1)) / denom)

    return score


def search(index, query, k=8):
    query_tokens = tokenize_kr(query)

    scored = []
    for i, doc_tokens in enumerate(index["doc_tokens"]):
        score = bm25_score(
            query_tokens=query_tokens,
            doc_tokens=doc_tokens,
            doc_freq=index["doc_freq"],
            num_docs=index["num_docs"],
            avg_doc_len=index["avg_doc_len"],
        )
        scored.append((i, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_idx = [i for i, score in scored[:k] if score > 0]

    if not top_idx:
        top_idx = [i for i, _ in scored[:k]]

    results = []
    for i in top_idx:
        results.append(
            f"[근거문서: {os.path.basename(index['meta'][i])}]\n{index['chunks'][i]}"
        )
    return results


def preview_anonymized(contract_path):
    try:
        if not contract_path:
            return "계약서 파일을 먼저 업로드하세요.", "", ""
        raw_text = extract_text(contract_path)
        if not raw_text.strip():
            return "텍스트를 추출하지 못했습니다.", "", ""
        anon_text, mapping, _ = anonymize_contract_text(raw_text)
        anon_text = post_fix_alias_leaks(anon_text, mapping)
        leak_candidates = find_possible_leaks(anon_text)
        mapping_lines = [f"{label} = {original}" for original, label in sorted(mapping.items(), key=lambda x: (x[1], x[0]))]
        preview_text = anon_text[:12000]
        mapping_text = "\n".join(mapping_lines[:400]) if mapping_lines else "치환된 항목 없음"
        leak_text = "\n".join(leak_candidates[:100]) if leak_candidates else "추가 누락 후보 없음"
        return preview_text, mapping_text, leak_text
    except Exception as e:
        return f"오류: {e}", "", ""


def validate_ground_lines(result_text: str):
    lines = result_text.splitlines()
    invalid = []
    for line in lines:
        if line.strip().startswith("문제가 되는 근거:"):
            content = line.split("문제가 되는 근거:", 1)[-1].strip()
            has_doc_name = any(keyword in content for keyword in ["법", "규정", "지침", "규칙", "내규", "기준", "운영지침"])
            has_article = bool(re.search(r'제\s*\d+\s*조|제\s*\d+\s*항|제\s*\d+\s*호', content))
            if not (has_doc_name and has_article):
                invalid.append(line)
    return invalid

import re

def highlight_revisions(text: str) -> str:
    return re.sub(
        r"<chg>(.*?)</chg>",
        r'<span style="color:#1f77b4; font-weight:700;">\1</span>',
        text,
        flags=re.DOTALL
    )

def analyze_contract(
    contract_path: str,
    use_anonymization: bool = True,
    restore_names: bool = False,
    zip_path: str | None = None,
    zip_link: str | None = None,
) -> str:
    try:
        if not contract_path:
            return "오류: 검토할 계약서 파일을 업로드하세요."

        # 1) 대학 내부규정/축적 문서 RAG
        index = load_internal_index(zip_path=zip_path, zip_link=zip_link)

        contract_text_raw = extract_text(contract_path)
        if not contract_text_raw.strip():
            return "오류: 계약서에서 텍스트를 추출하지 못했습니다. 스캔 PDF일 가능성이 있습니다."

        reverse_mapping = {}
        if use_anonymization:
            contract_text, mapping, reverse_mapping = anonymize_contract_text(contract_text_raw)
            contract_text = post_fix_alias_leaks(contract_text, mapping)
        else:
            contract_text = contract_text_raw

        # 2) GPT-5.6 Terra: 계약 유형/쟁점/법령 검색어 구조화
        intake = analyze_contract_intake(contract_text[:12000])
        contract_type = str(intake.get("contract_type", "기타/혼합형 계약"))
        issue_tags = [str(x) for x in intake.get("issue_tags", [])]
        law_queries = [str(x) for x in intake.get("law_queries", [])]

        local_query = str(intake.get("local_rag_query") or "").strip()
        if not local_query:
            local_query = " ".join(issue_tags)

        retrieval_query = (
            f"계약유형: {contract_type}\n"
            f"핵심쟁점: {', '.join(issue_tags)}\n"
            f"내부규정 검색어: {local_query}\n\n"
            + contract_text[:5000]
        )
        local_refs = "\n\n".join(search(index, retrieval_query, k=8))

        # 3) 국가법령정보 Open API: 현행법령/행정규칙/판례/법령해석례 공식 근거 조회
        official = collect_legal_evidence(
            law_queries or issue_tags,
            max_items=8,
            max_queries=4,
        )
        official_refs = str(official.get("evidence") or "").strip()
        openlaw_warning = str(official.get("warning") or "").strip()

        evidence_sections = []
        if official_refs:
            evidence_sections.append(
                "[A. 국가법령정보센터 공식 근거]\n"
                + official_refs
            )
        else:
            evidence_sections.append(
                "[A. 국가법령정보센터 공식 근거]\n"
                + "공식 API 근거가 제공되지 않았음. 외부 법령을 기억에 의존해 인용하지 말 것."
            )

        evidence_sections.append(
            "[B. 대학 내부규정/축적 문서 RAG]\n"
            + (local_refs or "검색된 내부/축적 문서 근거 없음")
        )
        all_evidence = "\n\n".join(evidence_sections)

        source_status = (
            "국가법령정보 Open API 사용"
            if official.get("enabled")
            else "국가법령정보 Open API 미사용(키 미설정 또는 조회 불가)"
        )

        # 4) GPT-5.6 Sol: 최종 계약 검토
        prompt = f"""
너는 대학 산학협력단·기술사업화 조직의 계약 검토를 지원하는 법률 검토 AI다.
최종 법률자문이 아니라 내부 실무 검토 초안을 작성한다.

[1차 분류 결과]
- 계약 유형: {contract_type}
- 핵심 쟁점: {", ".join(issue_tags) if issue_tags else "미분류"}
- 법령 검색어: {", ".join(law_queries) if law_queries else "미분류"}
- 데이터 소스 상태: {source_status}

가장 중요한 원칙:
1. 법령명, 조문번호, 판례, 법령해석례를 기억으로 만들어내지 않는다.
2. 공식 법률 근거는 반드시 [A. 국가법령정보센터 공식 근거]에 실제 포함된 내용에서만 인용한다.
3. 대학 내부규정·지침은 [B. 대학 내부규정/축적 문서 RAG]에서 실제 확인되는 경우에만 인용한다.
4. 표준계약서, 예규, 다른 공공기관 기준은 해당 계약에 직접 적용되는 법령이라고 단정하지 않는다.
5. "직접 적용 법령", "기관 내부 적용기준", "참고 가능한 기준/판례", "적용 여부 추가 확인 필요"를 구분한다.
6. 계약의 재원, 당사자의 법적 지위, 국가연구개발사업 해당 여부 등이 불명확하면 적용 여부를 단정하지 말고 추가 확인사항으로 표시한다.
7. 근거가 없는 일반적 계약실무 의견은 상세 법률근거 항목에 넣지 말고 "실무상 추가 권고"로 구분한다.
8. 계약서 원문을 인용할 때는 실제 원문에 있는 조항번호와 문구만 사용한다. 없는 조항번호를 생성하지 않는다.
9. 대학에 불리하다는 이유만으로 위법이라고 단정하지 않는다. 불리함, 법적 위험, 내부규정 충돌 가능성을 구분한다.

중점 검토:
- 계약 목적과 계약 유형의 적정성
- 연구비/용역대금 지급과 정산
- 지식재산권·연구성과 귀속
- 기존 보유기술(Background IP)과 신규성과 구분
- 직무발명 및 특허출원·비용부담
- 기술이전·실시권·우선협상권
- 논문·학회발표 및 연구성과 공개
- 비밀유지 범위와 기간
- 성과보증·비침해보증·적합성보증
- 손해배상·면책·책임한도
- 계약해지 및 기수행 비용 정산
- 투자계약인 경우 우선주, 상환/전환, 청산우선권, 희석방지, 동반/강제매도, 경영동의권, 창업자 의무, IP 관련 진술보장

조항 수정안 작성 원칙:
- 실제 계약서에 바로 반영 가능한 문장으로 작성
- 원 조항의 구조는 가능한 유지
- 위험 요소를 완화하는 최소 수정
- 새 의무를 임의로 만들지 않음
- 변경 핵심 부분만 <chg>변경 문구</chg>로 표시

[계약서]
{contract_text[:14000]}

[검토 근거]
{all_evidence[:50000]}

출력 형식:

## 📌 검토 요약

- **계약 유형 판단:**
- **핵심 쟁점:**
- **공식 법령정보 조회 상태:** {source_status}
- **전체 위험도:** 낮음 / 보통 / 높음 중 하나
- **핵심 총평:**

## ⚖️ 적용 근거 구분

### 직접 적용 검토 법령
- 근거에서 실제 확인되는 법령과 조문만 작성
- 직접 적용 여부가 확실하지 않으면 이 항목에 넣지 않음

### 기관 내부 적용기준
- 대학 내부규정/지침에서 실제 확인되는 조항만 작성

### 참고 가능한 기준·판례·법령해석
- 직접 적용 법령과 구분하여 작성

### 적용 여부 추가 확인 필요
- 계약 재원, 사업유형, 당사자 지위 등 추가 사실 확인이 필요한 사항

## 📌 상세 검토 결과

쟁점별로 아래 구조 반복:

### 1. 조항 제목 또는 유형

#### 📄 검토 대상 원문
- 제○조 제○항: 실제 계약서 원문

#### ⚠️ 검토 의견
- 대학 입장에서의 위험 또는 불균형
- 위법 여부와 계약상 불리함을 구분

#### ⚖️ 관련 근거
- 문서명 + 정확한 조문/사건번호/안건번호 + 근거 요지
- [검토 근거]에 없는 출처는 작성 금지

#### ✏️ 조항 변경 예시
- 제○조 제○항: 수정문안

---

## 📌 실무상 추가 권고
- 법률근거가 충분하지 않지만 협상·계약관리상 확인할 사항
- 추가로 필요한 자료 또는 사실관계

## ⚠️ 주의
본 결과는 내부 검토 참고용이며 최종 법률자문을 대체하지 않는다.
"""

        result_text = generate_final_review(prompt)

        if use_anonymization and restore_names:
            result_text = deanonymize_text(result_text, reverse_mapping)

        result_text = highlight_revisions(result_text)

        # 5) 최종 출력에 등장한 법령/규정 조문이 실제 전달된 근거에 있는지 1차 기계 검증
        unverified = find_unverified_citations(result_text, all_evidence)
        if unverified:
            result_text += "\n\n## 🔎 근거 자동검증 메모\n"
            result_text += (
                "- 아래 인용은 전달된 공식/내부 근거 텍스트에서 문서명과 조문번호의 동시 일치를 "
                "기계적으로 확인하지 못했습니다. 최종 사용 전 원문 확인이 필요합니다.\n"
            )
            for citation in unverified[:20]:
                result_text += f"- {citation}\n"

        if openlaw_warning:
            result_text += "\n\n## ℹ️ 법령 API 조회 메모\n"
            for line in openlaw_warning.splitlines()[:12]:
                result_text += f"- {line}\n"

        return result_text

    except Exception as e:
        return f"실행 중 오류가 발생했습니다.\n\n{type(e).__name__}: {e}"

