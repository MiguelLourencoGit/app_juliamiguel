
import streamlit as st
from pathlib import Path

st.title("Upload de PDF")

# Pasta onde está o projeto Git
PASTA_PROJETO = Path(__file__).parent

pdf = st.file_uploader("Escolhe um PDF", type=["pdf"])

if pdf is not None:
    caminho = PASTA_PROJETO / pdf.name

    caminho.write_bytes(pdf.getvalue())

    st.success(f"PDF guardado: {pdf.name}")
