import os, sys, time, traceback
from datetime import datetime
from io import BytesIO
from typing import Optional

# ================= AJUSTE DE PATH PARA IMPORTAR app.* =================
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# ======================================================================

import base64
import smtplib
import ssl
from email.message import EmailMessage

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

# ==== IMPORTA UTILITÁRIOS DO PROJETO (banco e arquivos) ====
from app.utils.db import (  # type: ignore
    salvar_processo,
    listar_processos,
    atualizar_status,
    registrar_relatorio,
    DATA_DIR,
    REL_DIR,
)

# ========= CONFIGURAÇÕES =========
# Local: lê .env. Cloud: Secrets já viram env vars.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

RELATORIOS_DIR = str(REL_DIR)
API_BASE = os.getenv("JUSREPORT_API_URL", "http://127.0.0.1:8000").rstrip("/")

EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE")
SENHA_APP = os.getenv("SENHA_APP")
SENHA_ADVOGADO = os.getenv("SENHA_ADVOGADO", "123cas#@!adv")

MAX_TEXT_CHARS_UI = int(os.getenv("MAX_TEXT_CHARS_UI", "60000"))

SUMARIZACOES_DISPONIVEIS = [
    "Execução",
    "Ação de Cobrança",
    "Ação Monitória",
    "Embargos à Execução",
    "Reintegração de Posse",
]

os.makedirs(RELATORIOS_DIR, exist_ok=True)

# ========= HELPERS =========
def _guess_mime(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/octet-stream"


def _api_request(method: str, path: str, *, timeout: int = 60, retries: int = 2, **kwargs):
    """
    Render Free pode dormir. Aqui a gente tenta algumas vezes com timeout alto.
    """
    url = f"{API_BASE}{path}"
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2.0 + attempt * 1.5)
            else:
                raise last_err


def api_health() -> dict:
    try:
        r = _api_request("GET", "/health", timeout=90, retries=2)
        data = r.json()
        data["api_reachable"] = True
        return data
    except Exception as e:
        return {"service": "jusreport-api", "api_reachable": False, "gemini_configured": False, "error": str(e)}


def api_export_docx(content_markdown: str, filename: str) -> bytes:
    r = _api_request(
        "POST",
        "/export/docx",
        timeout=180,
        retries=1,
        data={"content": content_markdown, "filename": filename},
    )
    return r.content


def api_summarize_text(text: str, case_number: str, action_type: str) -> dict:
    """
    CHAMADA PRINCIPAL (cloud-friendly): manda TEXTO para a API (sem upload de PDF).
    """
    payload = {"text": text, "case_number": case_number, "action_type": action_type}
    r = _api_request("POST", "/summarize_text", timeout=900, retries=0, json=payload)
    return r.json()


def extrair_texto_pdf_local(pdf_path: str, max_chars: int = 60000) -> str:
    """
    Extrai texto do PDF no próprio Streamlit (Cloud/local), evitando /ingest no Render.
    """
    try:
        import pdfplumber
    except Exception as e:
        raise RuntimeError(f"pdfplumber não está instalado no Streamlit: {e}")

    partes = []
    total = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                partes.append(t)
                total += len(t)
            if total >= max_chars:
                break

    texto = "\n\n".join(partes).strip()
    if len(texto) > max_chars:
        texto = texto[:max_chars]
    return texto


def enviar_email_cliente(destinatario: str, relatorio_path: str, numero_processo: str) -> None:
    if not EMAIL_REMETENTE or not SENHA_APP:
        st.warning("⚠️ Credenciais de e-mail não configuradas. Relatório NÃO foi enviado por e-mail.")
        return

    msg = EmailMessage()
    msg["Subject"] = "Seu Relatório JUSREPORT está pronto!"
    msg["From"] = EMAIL_REMETENTE
    msg["To"] = destinatario
    msg.set_content(
        f"Prezado(a),\n\nSegue em anexo o relatório do processo número {numero_processo}.\n\n"
        f"Atenciosamente,\nEquipe JUSREPORT\n"
    )

    with open(relatorio_path, "rb") as f:
        file_data = f.read()
        file_name = os.path.basename(relatorio_path)

    msg.add_attachment(
        file_data,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=file_name,
    )

    contexto = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=contexto) as smtp:
        smtp.login(EMAIL_REMETENTE, SENHA_APP)
        smtp.send_message(msg)


def exibir_logo_e_titulo_lado_a_lado() -> None:
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode()
        html = (
            '<div style="display:flex;align-items:center;margin-top:30px;">'
            f'<img src="data:image/png;base64,{encoded}" style="width:65px;margin-right:30px;" />'
            '<h1 style="margin:0;font-size:40px;">JUSREPORT</h1>'
            "</div>"
            '<div style="margin-top:20px;"><h3>Área do Cliente</h3></div>'
        )
        st.markdown(html, unsafe_allow_html=True)


# ========= BANCO (helpers) =========
def carregar_processos_pendentes_df() -> pd.DataFrame:
    rows = listar_processos(status="pendente")
    if not rows:
        return pd.DataFrame(columns=["id","nome_cliente","email","numero_processo","tipo","conferencia","data_envio","caminho_arquivo"])
    df = pd.DataFrame(rows)
    for c in ["id","nome_cliente","email","numero_processo","tipo","conferencia","data_envio","caminho_arquivo"]:
        if c not in df.columns:
            df[c] = None
    return df[["id","nome_cliente","email","numero_processo","tipo","conferencia","data_envio","caminho_arquivo"]].sort_values(by="data_envio", ascending=False)


def carregar_processos_finalizados_df() -> pd.DataFrame:
    rows = listar_processos(status="finalizado")
    if not rows:
        return pd.DataFrame(columns=["nome_cliente","email","numero_processo","data_envio","caminho_arquivo"])
    df = pd.DataFrame(rows)
    for c in ["nome_cliente","email","numero_processo","data_envio","caminho_arquivo"]:
        if c not in df.columns:
            df[c] = None
    return df[["nome_cliente","email","numero_processo","data_envio","caminho_arquivo"]].sort_values(by="data_envio", ascending=False)


def carregar_contagem_processos_mensal_df() -> pd.DataFrame:
    rows = listar_processos(status=None)
    if not rows:
        return pd.DataFrame(columns=["nome_cliente","email","mes_ano","quantidade"])
    df = pd.DataFrame(rows)
    df["data_envio"] = pd.to_datetime(df["data_envio"], errors="coerce")
    df["mes_ano"] = df["data_envio"].dt.strftime("%m/%Y")
    return (
        df.groupby(["nome_cliente", "email", "mes_ano"])
        .size()
        .reset_index(name="quantidade")
        .sort_values(by="mes_ano", ascending=False)
    )


def excluir_processo_e_arquivo(processo_id: str, caminho_arquivo: str) -> None:
    import sqlite3
    DB_PATH = os.path.join(str(DATA_DIR), "banco_dados.db")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM processos WHERE id = ?", (processo_id,))
    conn.commit()
    conn.close()
    if caminho_arquivo and os.path.exists(caminho_arquivo):
        try:
            os.remove(caminho_arquivo)
        except Exception:
            pass


def finalizar_processo_e_enviar(processo_id: str, relatorio_path: str, email_cliente: str, numero_processo: str) -> None:
    atualizar_status(processo_id, "finalizado")
    enviar_email_cliente(email_cliente, relatorio_path, numero_processo)


# ========= APP STREAMLIT =========
st.set_page_config(page_title="JusReport", page_icon="⚖️", layout="wide")

if not EMAIL_REMETENTE or not SENHA_APP:
    st.sidebar.info("⚠️ Configure EMAIL_REMETENTE e SENHA_APP (Secrets no Streamlit Cloud / .env local) para enviar e-mails.")

st.sidebar.title("Navegação")
pagina = st.sidebar.selectbox("Escolha a página", ["Área do Cliente", "Área Jusreport"])

# =====================================================================
# ÁREA DO CLIENTE
# =====================================================================
if pagina == "Área do Cliente":
    exibir_logo_e_titulo_lado_a_lado()

    with st.form("formulario_processo"):
        nome_cliente = st.text_input("Nome ou nome da empresa")
        email = st.text_input("E-mail para receber o relatório")
        numero = st.text_input("Número do processo")
        tipo = st.selectbox("Tipo de sumarização", SUMARIZACOES_DISPONIVEIS, index=0)
        conferencia = st.radio("Tipo de relatório desejado:", ["Conferido por um advogado", "Sem conferência"], index=0)
        arquivo = st.file_uploader("Anexar arquivo do processo (PDF, DOCX)", type=["pdf", "docx"])
        enviado = st.form_submit_button("Enviar processo")

        if enviado:
            if not (nome_cliente and email and numero and arquivo):
                st.warning("Por favor, preencha todos os campos obrigatórios.")
            else:
                try:
                    processo_id = salvar_processo(nome_cliente, email, numero, tipo, arquivo, conferencia)
                    st.success(f"Processo enviado com sucesso! ID: {processo_id}")
                except Exception as e:
                    st.error(f"Erro ao salvar processo: {e}")
                    with st.expander("📄 Detalhes técnicos (traceback)"):
                        st.code("".join(traceback.format_exception(type(e), e, e.__traceback__)))

# =====================================================================
# ÁREA INTERNA
# =====================================================================
else:
    st.title("Área Interna - JusReport")

    health = api_health()
    with st.expander("🔎 Debug /health da API", expanded=False):
        st.json(health)

    api_reachable = bool(health.get("api_reachable"))
    gemini_ok = bool(health.get("gemini_configured"))

    if not api_reachable:
        st.error(
            f"Não foi possível conectar na API em {API_BASE}. "
            f"Detalhe técnico: {health.get('error')}"
        )
    elif not gemini_ok:
        st.error("Gemini não está configurado na API (Render). Configure GEMINI_API_KEY no Render e redeploy.")

    if "auth_ok" not in st.session_state:
        st.session_state["auth_ok"] = False

    if not st.session_state["auth_ok"]:
        senha = st.text_input("Digite a senha de acesso:", type="password")
        if st.button("Entrar"):
            if senha == SENHA_ADVOGADO:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.warning("Senha incorreta.")
        st.stop()

    # -------- Processos Pendentes --------
    st.subheader("Processos Pendentes")

    try:
        df = carregar_processos_pendentes_df()
    except Exception as e:
        st.error("Falha ao acessar o banco SQLite. Verifique os Logs (Manage app → Logs).")
        st.code(str(e))
        st.stop()

    if df.empty:
        st.info("Nenhum processo pendente no momento.")
    else:
        for _, row in df.iterrows():
            st.markdown("---")
            st.markdown(f"**Cliente:** {row['nome_cliente']}")
            st.markdown(f"**E-mail:** {row['email']}")
            st.markdown(f"**Número do processo:** {row['numero_processo']}")
            st.markdown(f"**Tipo de sumarização:** {row['tipo']}")
            st.markdown(f"**Tipo de relatório:** {row['conferencia']}")

            data_fmt = row["data_envio"]
            try:
                data_fmt = pd.to_datetime(row["data_envio"]).strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass
            st.markdown(f"**Data de envio:** {data_fmt}")

            col1, col2, col3 = st.columns([2, 1, 1])

            caminho_cliente = row.get("caminho_arquivo")

            with col1:
                if caminho_cliente and os.path.exists(caminho_cliente):
                    with open(caminho_cliente, "rb") as file:
                        st.download_button(
                            label="Baixar arquivo do cliente",
                            data=file,
                            file_name=os.path.basename(caminho_cliente),
                            mime=_guess_mime(caminho_cliente),
                            key=f"download_{row['id']}",
                        )
                else:
                    st.warning("Arquivo original não encontrado no disco (provavelmente caminho antigo).")

            with col2:
                if st.button("Excluir", key=f"excluir_{row['id']}"):
                    try:
                        excluir_processo_e_arquivo(row["id"], caminho_cliente)
                        st.success(f"Processo de {row['nome_cliente']} excluído.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erro ao excluir: {e}")
                        with st.expander("📄 Detalhes técnicos (traceback)"):
                            st.code("".join(traceback.format_exception(type(e), e, e.__traceback__)))

            with col3:
                disabled = (not api_reachable) or (not gemini_ok) or (not caminho_cliente) or (not os.path.exists(caminho_cliente))
                if st.button("Processar automaticamente", key=f"processar_{row['id']}", disabled=disabled):
                    try:
                        log = st.expander("🔎 Log de processamento", expanded=True)

                        with st.spinner("Extraindo texto do PDF (no Streamlit)..."):
                            texto_pdf = extrair_texto_pdf_local(caminho_cliente, max_chars=MAX_TEXT_CHARS_UI)

                        if not texto_pdf.strip():
                            st.error("A IA não retornou conteúdo para o relatório (texto do PDF vazio/ilegível).")
                            st.stop()

                        with st.spinner("Gerando sumarização com IA (Render)..."):
                            sum_resp = api_summarize_text(
                                text=texto_pdf,
                                case_number=str(row["numero_processo"]),
                                action_type=str(row["tipo"]),
                            )

                        summary_md = (sum_resp.get("summary_markdown", "") or "").strip()
                        if not summary_md:
                            st.error("A IA não retornou conteúdo para o relatório.")
                            st.stop()

                        st.markdown("**Prévia do relatório:**")
                        st.markdown(summary_md)

                        nome_saida = f"Sum_{row['numero_processo']}.docx"
                        with st.spinner("Exportando relatório para DOCX..."):
                            docx_bytes = api_export_docx(summary_md, nome_saida)

                        caminho_relatorio = os.path.join(RELATORIOS_DIR, nome_saida)
                        with open(caminho_relatorio, "wb") as out:
                            out.write(docx_bytes)

                        registrar_relatorio(row["id"], caminho_docx=caminho_relatorio)

                        if str(row.get("conferencia", "")).strip().lower().startswith("sem"):
                            finalizar_processo_e_enviar(row["id"], caminho_relatorio, row["email"], str(row["numero_processo"]))
                            st.success("Relatório gerado, finalizado e enviado ao cliente!")
                        else:
                            st.success("Relatório gerado e salvo para conferência do advogado.")

                        st.rerun()

                    except requests.HTTPError as e:
                        try:
                            st.error(f"Falha na API: {e.response.json()}")
                        except Exception:
                            st.error(f"Falha na API: {e}")
                        with st.expander("📄 Detalhes técnicos (traceback)"):
                            st.code("".join(traceback.format_exception(type(e), e, e.__traceback__)))
                    except Exception as e:
                        st.error(f"Erro no processamento automático: {e}")
                        with st.expander("📄 Detalhes técnicos (traceback)"):
                            st.code("".join(traceback.format_exception(type(e), e, e.__traceback__)))

                if disabled:
                    st.caption("⚠️ Para processar: API ok + Gemini ok + arquivo precisa existir no servidor do Streamlit.")

    # -------- Relatórios Finalizados --------
    st.subheader("Relatórios Finalizados")
    df_finalizados = carregar_processos_finalizados_df()

    if df_finalizados.empty:
        st.info("Nenhum relatório finalizado encontrado ainda.")
    else:
        try:
            df_finalizados["data_envio"] = pd.to_datetime(df_finalizados["data_envio"]).dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            pass

        st.dataframe(df_finalizados.drop(columns=["caminho_arquivo"], errors="ignore"))

        # Export: tenta Excel (openpyxl), se não tiver, cai pra CSV
        try:
            output_finalizados = BytesIO()
            with pd.ExcelWriter(output_finalizados, engine="openpyxl") as writer:
                df_finalizados.drop(columns=["caminho_arquivo"], errors="ignore").to_excel(
                    writer, index=False, sheet_name="RelatoriosFinalizados"
                )
            st.download_button(
                label="Baixar Relatórios Finalizados (Excel)",
                data=output_finalizados.getvalue(),
                file_name="relatorios_finalizados.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            csv_bytes = df_finalizados.drop(columns=["caminho_arquivo"], errors="ignore").to_csv(index=False).encode("utf-8")
            st.download_button(
                label="Baixar Relatórios Finalizados (CSV)",
                data=csv_bytes,
                file_name="relatorios_finalizados.csv",
                mime="text/csv",
            )

    # -------- Relatório Mensal --------
    st.subheader("Relatório Mensal de Processos por Cliente")
    df_contagem = carregar_contagem_processos_mensal_df()
    if not df_contagem.empty:
        st.dataframe(df_contagem)

        try:
            output = BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df_contagem.to_excel(writer, index=False, sheet_name="RelatorioMensal")
            st.download_button(
                label="Baixar Relatório Mensal (Excel)",
                data=output.getvalue(),
                file_name="relatorio_mensal_processos.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            csv_bytes = df_contagem.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="Baixar Relatório Mensal (CSV)",
                data=csv_bytes,
                file_name="relatorio_mensal_processos.csv",
                mime="text/csv",
            )
    else:
        st.info("Nenhum processo enviado ainda para gerar o relatório.")
