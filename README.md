# 대학-기업 계약/협약서 검토 AI (Streamlit)

기존 Gradio/Colab 코드를 GitHub + Streamlit 배포용으로 정리한 버전입니다.

## 이번 버전의 핵심
- 기본값으로 **기존 법령/규정 Google Drive 링크**를 그대로 사용
- 필요할 때만 **직접 ZIP 업로드** 또는 **Drive 링크 변경** 가능
- 동일한 법령 ZIP을 반복 사용할 경우 `.cache`에 **임베딩 인덱스 캐시** 저장
- API 키는 코드에 하드코딩하지 않고 환경변수/Secrets로 관리

## 파일 구조

```bash
contract_review_streamlit/
├── app.py
├── contract_core.py
├── requirements.txt
├── .gitignore
├── .streamlit/
│   └── config.toml
└── README.md
```

## 로컬 실행

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="YOUR_API_KEY"
streamlit run app.py
```

Windows PowerShell:

```powershell
$env:GEMINI_API_KEY="YOUR_API_KEY"
streamlit run app.py
```

## Streamlit Cloud 배포
1. 이 프로젝트를 GitHub 저장소에 업로드합니다.
2. Streamlit Cloud에서 해당 저장소를 연결합니다.
3. App file path를 `app.py`로 지정합니다.
4. Settings > Secrets에 아래 중 하나를 등록합니다.

```toml
GEMINI_API_KEY="YOUR_API_KEY"
```

또는

```toml
GOOGLE_API_KEY="YOUR_API_KEY"
```

## 사용 방법
1. 기본 설정에서는 기존 Google Drive 법령 ZIP 링크를 그대로 사용합니다.
2. 계약서 파일(PDF / DOCX / TXT / MD)만 업로드합니다.
3. 필요하면 법령 소스를 다음 중 하나로 바꿀 수 있습니다.
   - 직접 ZIP 업로드
   - Drive 링크 직접 입력
4. `익명화 미리보기` 또는 `검토 실행` 버튼을 누릅니다.

## 주의사항
- 스캔 PDF는 텍스트 추출이 제대로 되지 않을 수 있습니다.
- Google Drive 링크는 공유 설정이 열려 있어야 합니다.
- 최초 실행 시 법령 ZIP 다운로드 및 임베딩 생성 때문에 시간이 걸릴 수 있습니다.
- 이후 동일 ZIP 재사용 시 캐시를 사용해 속도가 빨라집니다.
