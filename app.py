import os
import tempfile
from pathlib import Path

import streamlit as st

from contract_core import (
    ConfigError,
    ValidationError,
    analyze_contract,
    preview_anonymized,
)
from export_utils import build_docx_bytes, build_pdf_bytes, render_review_html

st.set_page_config(
    page_title="PNU 계약/협약서 검토",
    page_icon="📄",
    layout="wide",
)

for key, default in {
    "review_result": None,
    "review_docx": None,
    "review_pdf": None,
    "review_filename": "계약검토결과",
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def save_uploaded_file(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        return tmp.name


st.title("PNU 계약/협약서 검토")
st.caption("대학 내부규정 ZIP/RAG와 국가법령정보 Open API를 함께 사용해 계약서를 검토합니다.")

with st.sidebar:
    st.subheader("실행 환경")
    openai_key_exists = bool(os.getenv("OPENAI_API_KEY", "").strip())
    openlaw_key_exists = bool(
        os.getenv("OPEN_LAW_API_KEY", "").strip()
        or os.getenv("LAW_API_KEY", "").strip()
        or os.getenv("LAW_OPEN_API_KEY", "").strip()
    )
    terra_model = os.getenv("OPENAI_TERRA_MODEL", "gpt-5.6-terra")
    sol_model = os.getenv("OPENAI_SOL_MODEL", "gpt-5.6-sol")

    st.write(f"- OpenAI API Key: {'설정됨' if openai_key_exists else '미설정'}")
    st.write(f"- 국가법령정보 API: {'설정됨' if openlaw_key_exists else '미설정'}")
    st.write(f"- 1차 분류 모델: {terra_model}")
    st.write(f"- 최종 검토 모델: {sol_model}")
    st.write("- 지원 계약서 형식: PDF, DOCX, TXT, MD")
    st.write("- 내부규정 ZIP: 내용 해시 기반 자동 재인덱싱")

st.subheader("대학 내부규정/축적 문서 소스")
source_mode = st.radio(
    "내부규정 사용 방식",
    ["기본값 사용", "직접 ZIP 업로드", "외부 링크 사용"],
    horizontal=True,
)

law_zip = None
law_zip_link = None

if source_mode == "직접 ZIP 업로드":
    law_zip = st.file_uploader("법령/규정 ZIP 업로드", type=["zip"])
elif source_mode == "외부 링크 사용":
    law_zip_link = st.text_input("법령/규정 ZIP 링크 (Google Drive 등)")
else:
    st.info("저장소의 lawcollect.zip을 내부규정 RAG 소스로 사용합니다. ZIP을 교체하면 자동으로 새 인덱스를 생성합니다.")

st.subheader("계약서 설정")
col1, col2 = st.columns([1, 1])
with col1:
    contract_file = st.file_uploader("계약서 업로드", type=["pdf", "docx", "txt", "md"])
with col2:
    use_anonymization = st.checkbox("자동 익명화 적용", value=True)
    restore_names = st.checkbox("검토 결과에서 원래 이름 복원", value=True)
    st.caption("국가법령정보 API 키가 설정되어 있으면 현행법령·행정규칙·판례·법령해석례를 자동 조회합니다.")

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
        elif not openai_key_exists:
            st.error("OPENAI_API_KEY가 설정되지 않았습니다. Streamlit Secrets에 OpenAI API 키를 등록하세요.")
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

            report_stem = Path(contract_file.name).stem.strip() or "계약서"
            report_title = f"{report_stem} 계약 검토 결과"

            st.session_state["review_result"] = result
            st.session_state["review_docx"] = build_docx_bytes(result, report_title)
            st.session_state["review_pdf"] = build_pdf_bytes(result, report_title)
            st.session_state["review_filename"] = f"{report_stem}_계약검토결과"
    except (ValidationError, ConfigError) as e:
        st.error(str(e))
    except Exception as e:
        st.exception(e)


if st.session_state.get("review_result"):
    st.markdown(render_review_html(st.session_state["review_result"]), unsafe_allow_html=True)

    st.divider()
    st.subheader("검토결과 보고서 다운로드")
    st.caption("DOCX와 PDF 모두 실제 텍스트 문서로 생성되어 내용 선택·복사가 가능합니다.")

    download_col1, download_col2 = st.columns(2)
    with download_col1:
        st.download_button(
            "Word 문서(.docx) 다운로드",
            data=st.session_state["review_docx"],
            file_name=f"{st.session_state['review_filename']}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=True,
        )
    with download_col2:
        st.download_button(
            "PDF 문서(.pdf) 다운로드",
            data=st.session_state["review_pdf"],
            file_name=f"{st.session_state['review_filename']}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
