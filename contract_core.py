import os
import re
import zipfile
import traceback
import shutil
import time
import tempfile
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

import fitz
import numpy as np
from docx import Document
from google import genai
from google.genai import errors as genai_errors


GEN_MODELS = [
    "gemini-2.5-flash",
]
EMBED_MODEL = "gemini-embedding-001"


class ConfigError(RuntimeError):
    pass


class ValidationError(ValueError):
    pass


# =========================================================
# 0. 클라이언트 / 설정
# =========================================================

def get_api_key() -> str:
    api_key = (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        raise ConfigError(
            "GEMINI_API_KEY 또는 GOOGLE_API_KEY 환경변수가 비어 있습니다. "
            "Streamlit Cloud의 Secrets 또는 로컬 환경변수에 API 키를 설정하세요."
        )
    return api_key


def get_client() -> genai.Client:
    return genai.Client(api_key=get_api_key())


# =========================================================
# 1. 법령 zip 경로 검증
# =========================================================

def validate_zip_path(zip_path: str) -> str:
    if not zip_path or not str(zip_path).strip():
        raise ValidationError("법령/규정 zip 파일 경로가 비어 있습니다.")

    zip_path = str(zip_path).strip()
    if not os.path.exists(zip_path):
        raise ValidationError(f"zip 파일을 찾을 수 없습니다: {zip_path}")

    if not zipfile.is_zipfile(zip_path):
        raise ValidationError("업로드한 파일이 유효한 zip 형식이 아닙니다.")

    return zip_path


# =========================================================
# 2. 파일 텍스트 추출
# =========================================================

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


# =========================================================
# 3. 텍스트 정리 / 청크
# =========================================================

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


# =========================================================
# 4. 범용 자동 익명화
# =========================================================

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

        variants = [
            token,
            f"'{token}'",
            f'"{token}"',
            f"‘{token}’",
            f"“{token}”",
        ]

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

        if any(k in entity_n for k in ["주식회사", "㈜", "회사", "Inc", "Ltd", "Corp"]):
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
        r'(?P<entity>[^\n()]{2,180}?)\s*\(\s*이하\s*[\'"“”‘’](?P<alias>[^\'"“”‘’]{1,50})[\'"“”‘’]\s*이라?\s*한다\s*\)',
        r'(?P<entity>[^\n()]{2,180}?)\s*\(\s*이하\s*[\'"“”‘’](?P<alias>[^\'"“”‘’]{1,50})[\'"“”‘’]\s*\)',
        r'(?P<entity>[^\n()]{2,180}?)\s*이하\s*[\'"“”‘’](?P<alias>[^\'"“”‘’]{1,50})[\'"“”‘’]\s*이라?\s*한다',
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

    quoted_tokens = re.findall(r'["\'“‘]([^"\'”’\n]{2,50})["\'”’]', text)
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
            if not val or is_generic_noun(val):
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



def post_fix_alias_leaks(text: str, mapping: Dict[str, str]) -> str:
    for token, label in sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True):
        token_n = token.strip()
        variants = [
            token_n,
            f"'{token_n}'",
            f'"{token_n}"',
            f"‘{token_n}’",
            f"“{token_n}”",
        ]
        for v in variants:
            text = text.replace(v, label)

    text = re.sub(r'["\'“‘](\[[^\]]+\])["\'”’]', r"\1", text)
    return text



def deanonymize_text(text: str, reverse_mapping: Dict[str, str]) -> str:
    if not reverse_mapping:
        return text

    for label, original in sorted(reverse_mapping.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(label, original)

    return text



def find_possible_leaks(text: str) -> List[str]:
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


# =========================================================
# 5. 임베딩 / 검색
# =========================================================

def embed(texts: List[str], batch_size: int = 50) -> np.ndarray:
    client = get_client()
    all_vecs = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        res = client.models.embed_content(model=EMBED_MODEL, contents=batch)
        batch_vecs = [e.values for e in res.embeddings]
        all_vecs.extend(batch_vecs)

    return np.array(all_vecs, dtype=np.float32)



def build_index(zip_path: str):
    zip_path = validate_zip_path(zip_path)

    work_dir = tempfile.mkdtemp(prefix="law_unzip_")

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(work_dir)

    files = []
    for root, _, fs in os.walk(work_dir):
        for name in fs:
            if name.lower().endswith((".pdf", ".docx", ".txt", ".md")):
                files.append(os.path.join(root, name))

    if not files:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ValidationError("zip 내부에서 읽을 수 있는 PDF/DOCX/TXT/MD 파일을 찾지 못했습니다.")

    chunks = []
    meta = []

    try:
        for f in files:
            try:
                text = extract_text(f)
                cs = chunk_text(text, size=1200, overlap=200)
                for c in cs:
                    chunks.append(c)
                    meta.append(f)
            except Exception:
                continue

        if not chunks:
            raise ValidationError("법령/규정 zip에서 텍스트를 추출하지 못했습니다.")

        emb = embed(chunks, batch_size=50)
        return {"chunks": chunks, "meta": meta, "emb": emb, "work_dir": work_dir}
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise



def cleanup_index(index: dict) -> None:
    work_dir = index.get("work_dir")
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)



def search(index, query: str, k: int = 8) -> List[str]:
    qv = embed([query], batch_size=1)[0]

    emb = index["emb"]
    qn = np.linalg.norm(qv) + 1e-12
    en = np.linalg.norm(emb, axis=1) + 1e-12

    sims = (emb @ qv) / (en * qn)
    idx = np.argsort(-sims)[:k]

    results = []
    for i in idx:
        results.append(f"[근거문서: {os.path.basename(index['meta'][i])}]\n{index['chunks'][i]}")
    return results


# =========================================================
# 6. 생성 재시도
# =========================================================

def generate_with_retry(prompt: str, max_retries: int = 6):
    client = get_client()
    last_error = None

    for model_name in GEN_MODELS:
        for attempt in range(max_retries):
            try:
                return client.models.generate_content(model=model_name, contents=prompt)
            except genai_errors.ServerError as e:
                last_error = e
                msg = str(e)
                if "503" in msg or "UNAVAILABLE" in msg or "high demand" in msg.lower():
                    wait_sec = min(2 ** attempt, 30)
                    time.sleep(wait_sec)
                    continue
                raise
            except Exception as e:
                last_error = e
                break

    raise last_error


# =========================================================
# 7. 미리보기
# =========================================================

def preview_anonymized(contract_path: str):
    if not contract_path:
        raise ValidationError("계약서 파일을 먼저 업로드하세요.")

    raw_text = extract_text(contract_path)
    if not raw_text.strip():
        raise ValidationError("텍스트를 추출하지 못했습니다.")

    anon_text, mapping, _ = anonymize_contract_text(raw_text)
    anon_text = post_fix_alias_leaks(anon_text, mapping)
    leak_candidates = find_possible_leaks(anon_text)

    mapping_lines = [f"{label} = {original}" for original, label in sorted(mapping.items(), key=lambda x: (x[1], x[0]))]

    preview_text = anon_text[:12000]
    mapping_text = "\n".join(mapping_lines[:400]) if mapping_lines else "치환된 항목 없음"
    leak_text = "\n".join(leak_candidates[:100]) if leak_candidates else "추가 누락 후보 없음"

    return preview_text, mapping_text, leak_text


# =========================================================
# 8. 근거 라인 검증
# =========================================================

def validate_ground_lines(result_text: str) -> List[str]:
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


# =========================================================
# 9. 계약 분석
# =========================================================

def analyze_contract(
    zip_path: str,
    contract_path: str,
    use_anonymization: bool = True,
    restore_names: bool = False,
) -> str:
    index = None
    try:
        zip_path = validate_zip_path(zip_path)
        if not contract_path:
            raise ValidationError("검토할 계약서 파일을 업로드하세요.")

        index = build_index(zip_path)
        contract_text_raw = extract_text(contract_path)

        if not contract_text_raw.strip():
            raise ValidationError("계약서에서 텍스트를 추출하지 못했습니다. 스캔 PDF일 수 있습니다.")

        reverse_mapping = {}
        if use_anonymization:
            contract_text, mapping, reverse_mapping = anonymize_contract_text(contract_text_raw)
            contract_text = post_fix_alias_leaks(contract_text, mapping)
        else:
            contract_text = contract_text_raw

        retrieval_query = """
대학과 기업 간 계약 검토.
중점 검토:
- 지식재산권 귀속
- 연구성과물 소유권
- 공동연구 결과물 귀속
- 기업 단독귀속
- 무상양도
- 독점 실시권
- 논문/성과 공개 제한
- 대학에 불리한 조항
- 대학의 과도한 보증 조항
- 제3자 지식재산권 비침해 보증
- 손해배상 책임 전가
- 면책 불균형
- 결과물의 완전성, 적합성, 비침해성 보증
- \"일체의 책임\", \"보증\", \"배상\", \"침해하지 않음\" 표현
""" + "\n\n" + contract_text[:6000]

        refs = "\n\n".join(search(index, retrieval_query, k=8))

        prompt = f"""
너는 대학 산학협력 계약 검토 전문가다.

다음 계약서를 대학 입장에서 검토하라.
특히 대학에 불리할 수 있는 조항을 빠짐없이 식별하되, 반드시 제공된 관련 규정 및 법령 발췌에서 직접 근거를 확인할 수 있는 경우에만 문제 조항으로 제시하라.

중점 검토 항목:
1. 지식재산권 귀속
2. 연구성과물 소유권
3. 공동연구 결과물의 공동소유 여부
4. 기업 단독귀속 또는 무상양도
5. 독점적 실시권, 우선협상권
6. 논문 발표 및 성과공개 제한
7. 대학의 과도한 보증 조항
8. 제3자 지식재산권 비침해 보증 조항
9. 손해배상, 면책, 책임 전가 조항
10. 대학의 고의·중과실과 무관하게 책임을 부담시키는 조항
11. \"일체의 책임\", \"보증한다\", \"침해하지 않음을 보장한다\", \"배상한다\" 등의 표현이 포함된 조항
12. 기업의 후속 사용행위까지 대학이 책임지도록 해석될 수 있는 조항

가장 중요한 출력 원칙:
- 반드시 [관련 규정 및 법령 발췌]에 포함된 내용에 근거하여 판단하라.
- \"문제가 되는 근거\"에는 반드시 실제 문서명을 특정하고, 가능하면 조문/조항/항목 번호까지 명시하라.
- 예시 형식:
  - 부산대학교 지식재산권 규정 제4조
  - 국가연구개발혁신법 제16조
  - 산학협력단 계약운영지침 제12조 제3항
- 근거 문서명이나 조문 번호를 특정할 수 없으면 그 항목은 출력하지 말라.
- 일반론, 추정, 관행, 취지만으로는 문제 조항으로 제시하지 말라.
- \"추가 확인 필요\"는 쓸 수 있지만, 그 경우에도 왜 근거 특정이 어려운지 설명해야 하며, \"상세 검토 결과\" 항목으로는 올리지 말고 [추가 권고]에만 적어라.
- 즉, [상세 검토 결과]에는 실제 근거 문서가 특정되는 항목만 포함하라.

검토 방식:
- 문제가 되는 조항은 계약서 원문 문장을 가능한 그대로 인용하라.
- 각 항목은 계약서 조항 + 실제 근거 문서 + 수정 예시가 모두 있어야 한다.
- 근거 문서가 없는 항목은 제외하라.
- 지식재산권 또는 책임·보증 관련 조항은 우선 검토하라.
- 마크다운 문법(**, ##, ---, 백틱 등)을 사용하지 말고 일반 텍스트로만 작성하라.
- 과장된 수사 표현 대신 실무 검토 문체로 작성하라.
- 답변은 한국어로만 작성하라.

[계약서]
{contract_text[:14000]}

[관련 규정 및 법령 발췌]
{refs}

출력 형식:
[검토 요약]
전체 위험도:
핵심 총평:

[상세 검토 결과]
1. 조항 제목 또는 유형
문제가 될만한 조항 원문:
문제가 되는 이유:
문제가 되는 근거: 문서명 + 조문/조항/항목 번호를 반드시 포함
바람직한 조항 변경 예시:

2. 조항 제목 또는 유형
문제가 될만한 조항 원문:
문제가 되는 이유:
문제가 되는 근거: 문서명 + 조문/조항/항목 번호를 반드시 포함
바람직한 조항 변경 예시:

[추가 권고]
- 근거 문서가 부족하여 상세 검토 결과에는 포함하지 않았지만 추가 검토가 필요한 사항
- 필요한 추가 확인 문서 또는 규정명

[주의]
본 결과는 내부 검토 참고용이며 최종 법률자문을 대체하지 않음.
"""

        res = generate_with_retry(prompt)
        result_text = res.text if hasattr(res, "text") else str(res)

        invalid_ground_lines = validate_ground_lines(result_text)
        if invalid_ground_lines:
            result_text += "\n\n[시스템 점검 메모]\n"
            result_text += "아래 항목은 근거 문서명 또는 조문 번호가 불충분할 수 있으므로 재검토 필요:\n"
            result_text += "\n".join(invalid_ground_lines[:20])

        if use_anonymization and restore_names:
            result_text = deanonymize_text(result_text, reverse_mapping)

        return result_text
    except Exception as e:
        return f"실행 중 오류가 발생했습니다.\n\n{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
    finally:
        if index:
            cleanup_index(index)
