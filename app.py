import os
import tempfile
from pathlib import Path

import streamlit as st

from contract_core import (
    ConfigError,
    DEFAULT_LAW_ZIP_LINK,
    ValidationError,
    analyze_contract,
    preview_anonymized,
)

st.set_page_config(
    page_title="PNU 계약/협약서 검토",
    page_icon="📄",
    layout="wide",
)


def save_uploaded_file(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        return tmp.name


st.title("PNU 계약/협약서 검토")
st.caption("기본값은 기존 법령/규정을 그대로 사용합니다. 필요할 때만 ZIP 업로드 또는 링크 변경을 하면 됩니다.")

with st.sidebar:
    st.subheader("실행 환경")
    gemini_key_exists = bool(os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip())
    st.write(f"- Gemini API Key 설정 여부: {'설정됨' if gemini_key_exists else '미설정'}")
    st.write("- 지원 계약서 형식: PDF, DOCX, TXT, MD")
    st.write("- 법령 ZIP 기본값: 기존 Google Drive 링크 사용")
    st.write("- 법령 인덱스: 동일 ZIP 재사용 시 캐시 사용")

st.subheader("법령/규정 소스")
source_mode = st.radio(
    "사용 방식 선택",
    ["기본 Drive 링크 사용", "직접 ZIP 업로드", "Drive 링크 직접 입력"],
    horizontal=True,
)

law_zip = None
law_zip_link = DEFAULT_LAW_ZIP_LINK

if source_mode == "직접 ZIP 업로드":
    law_zip = st.file_uploader("법령/규정 ZIP 업로드", type=["zip"])
elif source_mode == "Drive 링크 직접 입력":
    law_zip_link = st.text_input("법령/규정 Google Drive 링크", value=DEFAULT_LAW_ZIP_LINK)
else:
    st.text_input("기본 Google Drive 링크", value=DEFAULT_LAW_ZIP_LINK, disabled=True)

st.subheader("계약서 설정")
col1, col2 = st.columns([1, 1])
with col1:
    contract_file = st.file_uploader("계약서 업로드", type=["pdf", "docx", "txt", "md"])
with col2:
    use_anonymization = st.checkbox("자동 익명화 적용", value=True)
    restore_names = st.checkbox("검토 결과에서 원래 이름 복원", value=False)

btn_col1, btn_col2 = st.columns(2)
preview_clicked = btn_col1.button("익명화 미리보기", use_container_width=True)
run_clicked = btn_col2.button("검토 실행", type="primary", use_container_width=True)

if preview_clicked:
    try:
        if contract_file is None:
            st.error("계약서를 먼저 업로드하세요.")
        else:
            contract_path = save_uploaded_file(contract_file)
            preview_text, mapping_text, leak_text = preview_anonymized(contract_path)
            st.subheader("익명화 미리보기")
            st.text_area("익명화된 계약서", preview_text, height=360)
            st.text_area("치환표", mapping_text, height=360)
            st.text_area("추가 누락 후보", leak_text, height=160)
    except (ValidationError, ConfigError) as e:
        st.error(str(e))
    except Exception as e:
        st.exception(e)

if run_clicked:
    try:
        if contract_file is None:
            st.error("계약서를 먼저 업로드하세요.")
        else:
            zip_path = save_uploaded_file(law_zip) if law_zip is not None else None
            contract_path = save_uploaded_file(contract_file)
            with st.spinner("계약서를 검토 중입니다..."):
                result = analyze_contract(
                    zip_path=zip_path,
                    zip_link=law_zip_link,
                    contract_path=contract_path,
                    use_anonymization=use_anonymization,
                    restore_names=restore_names,
                )
            st.subheader("검토 결과")
            st.text_area("결과", result, height=700)
    except (ValidationError, ConfigError) as e:
        st.error(str(e))
    except Exception as e:
        st.exception(e)

with st.expander("Streamlit Cloud 설정 방법"):
    st.markdown(
        """
        1. GitHub 저장소에 이 프로젝트를 업로드합니다.
        2. Streamlit Cloud에서 저장소를 연결합니다.
        3. App file path를 `app.py`로 지정합니다.
        4. Settings > Secrets에 아래 중 하나를 추가합니다.
           - `GEMINI_API_KEY=...`
           - `GOOGLE_API_KEY=...`
        5. 기본값으로는 기존 법령 Google Drive 링크를 그대로 사용합니다.
        6. 같은 법령 ZIP을 반복 사용하면 `.cache`의 인덱스를 재사용합니다.
        """
    )
