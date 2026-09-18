# 대학-기업 계약/협약서 검토 AI (Streamlit)

대학 내부규정 RAG와 국가법령정보 Open API를 결합해 계약·협약서 조항을 검토하는 Streamlit 앱입니다.

## 현재 구조

- **GPT-5.6 Terra**: 계약 유형 분류, 핵심 쟁점 추출, 법령 검색어 생성
- **국가법령정보 Open API**: 현행법령, 행정규칙, 판례, 법령해석례 공식 근거 조회
- **내부규정 RAG**: `lawcollect.zip`의 부산대학교/산학협력단 규정·지침·축적 문서 검색
- **GPT-5.6 Sol**: 핵심 독소/위험조항 중심으로 최종 검토 및 수정 문안 작성
- **간소화된 결과**: 각 위험조항을 원문 → 문제되는 이유 → 근거 → 변경 예시 순서로 표시
- **근거 자동검증**: 결과에 인용된 법령/규정 조문을 서버에서 1차 점검하되 사용자 본문에는 진단 로그를 노출하지 않음
- **보고서 다운로드**: 검토 결과를 실제 텍스트 기반 DOCX/PDF로 생성하여 복사·편집·공유 가능

## 내부규정 ZIP 동작

기존 `lawcollect.zip` 구조를 그대로 사용합니다.

```text
lawcollect.zip
  ├─ 부산대학교 규정
  ├─ 산학협력단 규정
  ├─ 지식재산/기술이전 관련 규정
  ├─ 연구계약/연구비 관련 지침
  └─ 기타 내부 기준
```

ZIP 파일의 실제 내용 SHA-256을 기준으로 인덱스를 캐시합니다. 따라서 ZIP 내용이 바뀌면 자동으로 새 인덱스를 생성하고, 내용이 같으면 기존 캐시를 재사용합니다.

구버전 호환을 위해 ZIP을 찾지 못하는 경우에만 기존 `law_index.pkl`을 fallback으로 사용합니다.

## 환경변수 / Streamlit Secrets

필수:

```toml
OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
```

국가법령정보 Open API 연동:

```toml
OPEN_LAW_API_KEY="YOUR_OPEN_LAW_OC_VALUE"
```

모델은 필요하면 변경할 수 있습니다.

```toml
OPENAI_TERRA_MODEL="gpt-5.6-terra"
OPENAI_SOL_MODEL="gpt-5.6-sol"
```

## 국가법령정보 API 사용 범위

1차 버전에서는 다음 공식 데이터를 조회합니다.

- 현행법령(시행일 기준): `target=eflaw`
- 현행 행정규칙: `target=admrul`
- 판례: `target=prec`
- 법령해석례: `target=expc`

각 검색 결과의 식별자를 이용해 본문 API를 다시 조회하고, 최종 검토 모델에는 실제 조회된 근거만 전달합니다.

API 키가 없거나 개별 조회가 실패해도 내부규정 RAG는 계속 동작하며, 결과에 API 조회 상태를 표시합니다.

## 파일 구조

```text
contractreview/
├── app.py
├── contract_core.py
├── law_api.py
├── openai_models.py
├── legal_validator.py
├── export_utils.py
├── requirements.txt
├── lawcollect.zip
├── law_index.pkl        # 구버전 fallback
└── README.md
```

## 로컬 실행

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
export OPEN_LAW_API_KEY="YOUR_OPEN_LAW_OC_VALUE"
streamlit run app.py
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
$env:OPEN_LAW_API_KEY="YOUR_OPEN_LAW_OC_VALUE"
streamlit run app.py
```

## 검토 흐름

```text
계약서 업로드
  ↓
텍스트 추출 / 선택적 익명화
  ↓
GPT-5.6 Terra
- 계약 유형 분류
- 핵심 쟁점 추출
- 법령 검색어 생성
  ↓
국가법령정보 Open API + 내부규정 RAG
  ↓
GPT-5.6 Sol
- 주요 독소/위험조항 선별
- 문제되는 이유·근거 정리
- 조항 변경 예시 작성
  ↓
법령/규정 인용 자동검증
  ↓
DOCX / PDF 보고서 다운로드
```

## 주의사항

- 본 결과는 내부 검토 참고용이며 최종 법률자문을 대체하지 않습니다.
- 스캔 PDF는 텍스트 추출이 제대로 되지 않을 수 있습니다.
- 국가법령정보 API의 실제 적용 가능 여부는 계약 재원, 당사자 지위, 사업유형 등에 따라 달라질 수 있으므로 시스템은 직접 적용 법령과 참고 기준을 구분하도록 설계되어 있습니다.
- 공식 API에서 확인되지 않은 법령명·조문번호를 모델이 임의로 근거로 사용하지 않도록 프롬프트와 사후검증을 함께 적용합니다.

## 검토 결과 형식

기본 화면은 복잡한 법령 설명보다 실제 수정이 필요한 조항을 우선합니다.

```text
검토 요약
  ↓
독소/위험조항 1
- 독소조항 원문
- 문제되는 이유
- 근거
- 조항 변경 예시
  ↓
독소/위험조항 2 ...
```

독소/위험조항은 중요도 중심으로 최대 8개까지 제시하며, 서로 연결된 조항은 하나의 항목으로 묶습니다.

## 보고서 다운로드

검토 완료 후 화면 하단에서 다음 파일을 바로 다운로드할 수 있습니다.

- Word 문서(.docx): 문구 수정, 복사, 내부 결재자료 활용
- PDF 문서(.pdf): 실제 텍스트 레이어 기반으로 생성되어 검색 및 복사 가능
