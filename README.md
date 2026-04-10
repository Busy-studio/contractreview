# 대학-기업 계약/협약서 검토 AI (Streamlit)

업로드해 주신 Gradio 기반 코드를 Streamlit 배포용 구조로 재정리한 버전입니다.

## 포함 파일
- `app.py`: Streamlit 메인 앱
- `contract_core.py`: 계약 검토 로직
- `requirements.txt`: 패키지 목록
- `.gitignore`: 업로드 제외 파일 목록
- `.streamlit/config.toml`: Streamlit 기본 설정

## 1) 로컬 실행
```bash
pip install -r requirements.txt
```

환경변수 설정 후 실행합니다.

### macOS / Linux
```bash
export GEMINI_API_KEY="YOUR_API_KEY"
streamlit run app.py
```

### Windows PowerShell
```powershell
$env:GEMINI_API_KEY="YOUR_API_KEY"
streamlit run app.py
```

## 2) Streamlit Cloud 배포
1. 이 폴더 전체를 GitHub 저장소에 업로드합니다.
2. Streamlit Cloud에서 해당 저장소를 연결합니다.
3. Main file path를 `app.py`로 설정합니다.
4. Streamlit Cloud의 `Secrets`에 아래 중 하나를 입력합니다.
   ```toml
   GEMINI_API_KEY="YOUR_API_KEY"
   ```
   또는
   ```toml
   GOOGLE_API_KEY="YOUR_API_KEY"
   ```
5. Deploy 후 사용합니다.

## 3) 사용 방법
1. 법령/규정 파일들을 하나의 ZIP으로 압축합니다.
2. 앱에서 ZIP 파일을 업로드합니다.
3. 검토할 계약서를 업로드합니다.
4. 필요하면 `자동 익명화 적용`을 켭니다.
5. `익명화 미리보기` 또는 `검토 실행`을 누릅니다.

## 4) 원본 코드 대비 변경 사항
- Gradio UI → Streamlit UI로 전환
- Google Drive 링크 다운로드 방식 제거
- 법령 ZIP을 직접 업로드하는 방식으로 변경
- 하드코딩된 API 키 제거
- `GEMINI_API_KEY` / `GOOGLE_API_KEY` 환경변수 사용
- 임시 폴더 정리 로직 추가
- GitHub/Streamlit 배포에 맞는 파일 구조로 정리

## 5) 주의 사항
- 스캔 PDF처럼 텍스트 추출이 어려운 문서는 정상 분석되지 않을 수 있습니다.
- 결과는 내부 검토 참고용이며 최종 법률자문을 대체하지 않습니다.
- 업로드하는 법령/규정 ZIP의 품질에 따라 근거 문서 탐색 정확도가 달라집니다.
