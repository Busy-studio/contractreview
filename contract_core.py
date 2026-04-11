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
from google import genai
from google.genai import errors as genai_errors

GEN_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
]
DEFAULT_LAW_ZIP_LINK = "https://drive.google.com/file/d/1Wu5sEWPwdH7AX2n_08ViNCEZtZnhsMwH/view?usp=sharing"
CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class ConfigError(RuntimeError):
    pass


class ValidationError(ValueError):
    pass


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

    local_zip_path = Path("lawcollect.zip")
    if local_zip_path.exists() and zipfile.is_zipfile(local_zip_path):
        return str(local_zip_path)

    link = (zip_link or DEFAULT_LAW_ZIP_LINK).strip()
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
    path = Path(zip_path)
    stat = path.stat()
    base = f"{path.resolve()}::{stat.st_size}::{int(stat.st_mtime)}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


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


def generate_with_retry(prompt, max_retries=7):
    client = get_client()
    last_error = None

    for model_name in GEN_MODELS:
        for attempt in range(max_retries):
            try:
                return client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )

            except genai_errors.ClientError as e:
                last_error = e
                msg = str(e)

                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    wait_sec = min(3 * (2 ** attempt), 60)
                    time.sleep(wait_sec)
                    continue

                raise

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

    if last_error:
        msg = str(last_error)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            raise RuntimeError(
                "현재 Gemini API 사용량 한도 또는 순간 요청량 한도에 도달했습니다. "
                "잠시 후 다시 실행해 주세요."
            )
        if "503" in msg or "UNAVAILABLE" in msg:
            raise RuntimeError(
                "현재 Gemini 모델 응답이 일시적으로 몰려 있습니다. 잠시 후 다시 실행해 주세요."
            )
        raise last_error

    raise RuntimeError("모델 응답 생성에 실패했습니다.")


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
        r"⟪(.*?)⟫",
        r'<span style="color:#1f77b4; font-weight:700;">\1</span>',
        text,
        flags=re.DOTALL
    )

def analyze_contract(contract_path: str, use_anonymization: bool = True, restore_names: bool = False,
                     zip_path: str | None = None, zip_link: str | None = None) -> str:
    try:
        if not contract_path:
            return "오류: 검토할 계약서 파일을 업로드하세요."

        resolved_zip_path = resolve_law_zip(zip_path=zip_path, zip_link=zip_link)
        index = build_index(resolved_zip_path)

        contract_text_raw = extract_text(contract_path)
        if not contract_text_raw.strip():
            return "오류: 계약서에서 텍스트를 추출하지 못했습니다. 스캔 PDF일 가능성이 있습니다."

        reverse_mapping = {}
        if use_anonymization:
            contract_text, mapping, reverse_mapping = anonymize_contract_text(contract_text_raw)
            contract_text = post_fix_alias_leaks(contract_text, mapping)
        else:
            contract_text = contract_text_raw

        retrieval_query = """
대학과 기업 간 계약 검토.

먼저 확인할 사항:
- 민간재원 기반 공동연구 계약인지 여부
- 일반 용역, 시험, 분석, 자문, 검증, 평가 계약인지 여부
- 기술이전, 실시권, 옵션, 우선협상권 관련 계약인지 여부

중점 검토:
- 지식재산권 귀속
- 연구성과물 소유권
- 공동연구 결과물 귀속
- 기업 단독귀속
- 무상양도
- 독점 실시권
- 우선협상권
- 논문/성과 공개 제한
- 대학에 불리한 조항
- 대학의 과도한 보증 조항
- 제3자 지식재산권 비침해 보증
- 손해배상 책임 전가
- 면책 불균형
- 결과물의 완전성, 적합성, 비침해성 보증
- "일체의 책임", "보증", "배상", "침해하지 않음" 표현
""" + "\n\n" + contract_text[:4000]

        refs = "\n\n".join(search(index, retrieval_query, k=6))

        prompt = f"""
너는 대학 산학협력 계약 검토를 전문으로 수행하는 법률 전문가다.
실무 계약 검토 기준에 따라 보수적이고 근거 중심으로 판단하라.

다음 계약서를 대학 입장에서 검토하라.
단순히 불리 조항을 나열하는 것이 아니라, 먼저 계약의 성격과 적용 가능한 법령 및 규정의 범위를 판단한 후 그 범위 내에서 검토하라.
대학에 불리할 수 있는 조항은 반드시 제공된 관련 규정 및 법령 발췌에서 근거를 확인할 수 있는 경우에만 제시하라.

가장 먼저 해야 할 판단:
- 계약서의 명칭, 목적, 당사자, 비용 부담 구조, 연구개발비 재원, 과제명, 협약 체계 등을 종합하여 계약의 성격을 판단하라.
- 아래 유형 중 해당되는 유형을 하나 이상 선택하여 정리하라.
  1. 민간재원 기반 공동연구 계약
  2. 일반 용역, 시험, 분석, 자문, 검증, 평가 계약
  3. 기술이전, 실시권, 옵션, 우선협상권 관련 계약
  4. 혼합형 계약

적용 법령 및 규정 판단 원칙:
- 계약과 관련된 법령 및 규정을 폭넓게 식별하되, 다음의 법적 위계에 따라 검토 기준을 설정하라:
  1. 민법 및 관련 상위 법령
  2. 지식재산권 관련 법령 등 개별 법률
  3. 공공계약 관련 기준 및 예규
  4. 대학 내부 규정 및 지침
- 대학 내부 규정은 상위 법령에 반하지 않는 범위에서 적용되는 기준으로 활용하라.
- 대학은 공공기관의 성격을 가지므로, 민간 계약이라 하더라도 공공계약 기준, 내부 규정, 민법, 지식재산권 관련 법령을 종합적으로 검토 기준으로 활용하라.
- 특정 법령의 형식적 적용 여부와 관계없이, 대학에 불리한 조항 판단을 위해 유사한 법령 및 규정의 취지와 기준을 참고할 수 있다.

중점 검토 항목:
1. 지식재산권 귀속
2. 연구성과물 소유권
3. 공동연구 결과물의 공동소유 여부
4. 기업 단독귀속 또는 무상양도
5. 독점적 실시권, 우선협상권
6. 논문 발표 및 성과공개 제한
7. 대학의 과도한 책임 부담 조항
8. 제3자 지식재산권 관련 책임 조항
9. 손해배상, 면책, 책임 전가 조항
10. 대학의 고의 또는 과실과 무관하게 책임을 부담시키는 조항
11. 포괄적 책임 또는 불명확한 책임 범위를 규정하는 조항
12. 기업의 후속 사용행위까지 대학의 책임으로 확장될 수 있는 조항

가장 중요한 출력 원칙:
- 모든 판단은 반드시 [관련 규정 및 법령 발췌]에 포함된 내용을 근거로 한다.
- "문제가 되는 근거"에는 문서명과 조문, 조항 또는 항목 번호를 명확히 제시한다.
- 예시:
  - 민법 제750조
  - 특허법 제33조
  - 부산대학교 지식재산권 규정 제4조
  - 산학협력단 계약운영지침 제12조 제3항
- 근거 문서와 조문을 특정할 수 있는 경우에만 검토 결과를 작성한다.
- 일반적인 해석이나 관행만으로는 검토 항목으로 포함하지 않는다.
- 적용 가능성이 불명확한 경우에는 해당 내용을 [추가 권고]로 구분하여 제시한다.

검토 방식:
- 먼저 계약 유형을 판단한다.
- 다음으로 계약과 관련성이 있는 법령 및 규정을 폭넓게 식별한다.
- "우선 적용 검토 법령/규정(복수)"에는 실제 검토에서 근거로 사용할 가능성이 높은 규정을 상위법부터 하위 규정 순으로 정리한다.

예시:
- 민법
- 특허법
- 저작권법
- 공공계약 관련 기준 또는 예규
- 부산대학교 지식재산권관리 및 기술사업화 추진에 관한 규정
- 산학협력단 계약운영지침
- 산학협력단 세부운영지침

- 이후 검토에서는 위 규정 중 실제 조문이 특정 가능한 경우에만 근거로 활용한다.
- 문제가 되는 조항은 계약서의 원문을 그대로 인용하고, 반드시 해당 조항 번호(제○조 제○항)를 함께 표시한다.
- 조항 번호를 확인할 수 있는 경우에만 해당 항목을 작성한다.

조항 수정 예시 작성 기준:
- 실제 대학-기업 산학협력 계약서에 반영 가능한 문장으로 작성한다.
- 기존 조항의 구조와 표현을 유지하면서 위험 요소를 완화하는 방향으로 수정한다.
- 당사자의 권리와 의무가 명확하게 드러나도록 작성한다.
- 책임 범위는 고의 또는 중대한 과실 등으로 명확하게 한정한다.
- 불확정적이거나 포괄적인 책임이 발생하지 않도록 표현을 구성한다.
- 결과의 완전성, 적합성, 비침해성 등을 단정하는 표현 대신, 연구 수행 범위와 책임 기준 중심으로 작성한다.
- 수정 예시는 새로운 의무를 추가하는 것이 아니라 기존 조항의 위험을 완화하는 수준으로 작성한다.
- 수정된 조항은 원 조항보다 대학에 불리해지지 않도록 한다.
- 조항 변경 예시에서 원문 대비 변경된 핵심 부분은 <<변경된 문구>> 형식으로 표시하라.
- 변경되지 않은 부분은 그대로 유지하고, 실제 수정된 부분만 표시하라.
- 강조 범위는 최소한으로 유지하고, 문장 전체를 감싸지 말고 핵심 어구만 표시하라.

작성 형식:
- 마크다운 문법을 사용하지 않고 일반 텍스트로 작성한다.
- 실무 검토 문체로 작성한다.
- 답변은 한국어로 작성한다.
- 각 항목은 Markdown 형식으로 작성하며, 제목과 본문 사이에는 반드시 한 줄 이상 줄바꿈을 적용하고, 목록은 블릿 기호를 사용하여 개조식으로 작성하라.
- 하나의 검토 항목에 여러 조항이 포함되는 경우, 각 조항은 반드시 별도의 블릿("- ")으로 나누어 한 줄씩 작성하라.
- "독소조항 원문", "관련 근거", "조항 변경 예시"는 문단형으로 이어 쓰지 말고 항목별 목록 형태로 작성하라.
- 각 블릿 항목 사이에는 줄바꿈을 유지하고, 하나의 줄에 둘 이상의 조항을 이어서 쓰지 말라.

[계약서]
{contract_text[:10000]}

[관련 규정 및 법령 발췌]
{refs}

출력 형식:

## 📌 검토 요약

- **계약 유형 판단:**  
- **우선 적용 검토 법령/규정(복수):**  
- **전체 위험도:**  
- **핵심 총평:**  


## 📌 상세 검토 결과

### 1. 조항 제목 또는 유형

#### 📄 독소조항 원문
- 제○조 제○항: (계약서 원문)
- 제○조 제○항: (계약서 원문)
- 제○조 제○항: (계약서 원문)

#### ⚠️ 독소조항인 이유
- 대학 입장에서 왜 불리한지 개조식으로 설명
- 권리 제한, 책임 확대, 불균형 요소 중심으로 정리

#### ⚖️ 관련 근거
- 문서명 + 조문/조항/항목 번호: 관련 내용
- 문서명 + 조문/조항/항목 번호: 관련 내용

#### ✏️ 조항 변경 예시
- 제○조 제○항: (수정 문안)
- 제○조 제○항: (수정 문안)


---

### 2. 조항 제목 또는 유형

#### 📄 독소조항 원문
- 제○조 제○항: (계약서 원문)
- 제○조 제○항: (계약서 원문)

#### ⚠️ 독소조항인 이유
- 대학 입장에서 왜 불리한지 개조식으로 설명
- 필요 시 2~3개 항목으로 정리

#### ⚖️ 관련 근거
- 문서명 + 조문/조항/항목 번호: 관련 내용

#### ✏️ 조항 변경 예시
- 제○조 제○항: (수정 문안)


---

## 📌 추가 권고

- 근거 문서가 부족하거나 적용 여부가 불명확하여 상세 검토 결과에는 포함하지 않았지만 추가 검토가 필요한 사항
- 필요한 추가 확인 문서 또는 규정명


## ⚠️ 주의

본 결과는 내부 검토 참고용이며 최종 법률자문을 대체하지 않는다.
"""

        time.sleep(1.0)
        res = generate_with_retry(prompt)
        result_text = res.text if hasattr(res, "text") else str(res)

        if use_anonymization and restore_names:
            result_text = deanonymize_text(result_text, reverse_mapping)

        result_text = highlight_revisions(result_text)

        invalid_ground_lines = validate_ground_lines(result_text)
        if invalid_ground_lines:
            result_text += "\n\n[시스템 점검 메모]\n"
            result_text += "아래 항목은 근거 문서명 또는 조문 번호가 불충분할 수 있으므로 재검토 필요:\n"
            result_text += "\n".join(invalid_ground_lines[:20])

        return result_text

    except Exception as e:
        return f"실행 중 오류가 발생했습니다.\n\n{type(e).__name__}: {e}"