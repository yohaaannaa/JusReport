import os, sys, time, traceback
from datetime import datetime

# ================= AJUSTE DE PATH PARA IMPORTAR app.* =================
# ui.py está em: JusReport/app/web/streamlit/ui.py
# Então o root do projeto é três níveis acima: JusReport
PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# ======================================================================

import base64
import smtplib
import ssl
from email.message import EmailMessage
from io import BytesIO
from typing import Optional

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

# ---- Defensivo: variável 'hora' para qualquer código legado que a use ----
hora = datetime.now().strftime("%H-%M-%S")

# ---- Tipos de sumarização disponíveis ----
SUMARIZACOES_DISPONIVEIS = [
    "Execução",
    "Ação de Cobrança",
    "Ação Monitória",
    "Embargos à Execução",
    "Reintegração de Posse",
]

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
# Local: carrega .env
# Streamlit Cloud: Secrets viram env vars, então funciona igual
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

RELATORIOS_DIR = str(REL_DIR)
API_BASE = (os.getenv("JUSREPORT_API_URL", "http://127.0.0.1:8000") or "").strip().rstrip("/")

# ========= AJUSTES INICIAIS =========
os.makedirs(RELATORIOS_DIR, exist_ok=True)

# ========= CARREGAR VARIÁVEIS SECRETAS =========
EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE")
SENHA_APP = os.getenv("SENHA_APP")
SENHA_ADVOGADO = os.getenv("SENHA_ADVOGADO", "123cas#@!adv")


# ========= FUNÇÕES =========
def _guess_mime(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/octet-stream"


def enviar_email_cliente(destinatario: str, relatorio_path: str, numero_processo: str) -> None:
    """
    Envia o .docx ao cliente. Se credenciais não estiverem configuradas, apenas avisa no UI.
    """
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


# --------- CHAMADAS À API (FastAPI) ---------
def api_health() -> dict:
    """
    Render Free pode 'dormir'. Faz 3 tentativas com timeout alto.
    Também evita falso negativo no primeiro request.
    """
    if not API_BASE:
        return {
            "service": "jusreport-api",
            "api_reachable": False,
            "gemini_configured": False,
            "error": "JUSREPORT_API_URL vazio (API_BASE vazio).",
        }

    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(f"{API_BASE}/health", timeout=90)
            r.raise_for_status()
            data = r.json()
            data["api_reachable"] = True
            data["attempt"] = attempt + 1
            data["api_base"] = API_BASE
            return data
        except Exception as e:
            last_err = str(e)
            time.sleep(3)

    return {
        "service": "jusreport-api",
        "api_reachable": False,
        "gemini_configured": False,
        "api_base": API_BASE,
        "error": last_err,
    }


def api_ingest(file_path: str, case_number: str, client_id: Optional[str] = None) -> dict:
    """
    Upload pode demorar (PDF grande).
    """
    url = f"{API_BASE}/ingest"
    with open(file_path, "rb") as f:
        files = [("files", (os.path.basename(file_path), f, _guess_mime(file_path)))]
        data = {"case_number": case_number}
        if client_id:
            data["client_id"] = client_id
        resp = requests.post(url, files=files, data=data, timeout=240)
    resp.raise_for_status()
    return resp.json()


def api_status(job_id: str) -> dict:
    url = f"{API_BASE}/status/{job_id}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def api_summarize(question: str, case_number: str, action_type: str, k: int = 100, return_json: bool = True) -> dict:
    """
    /summarize pode ser lento (várias chamadas no Gemini).
    """
    url = f"{API_BASE}/summarize"
    payload = {
        "question": question,
        "case_number": case_number,
        "k": k,
        "return_json": return_json,
        "action_type": action_type,
    }
    resp = requests.post(url, json=payload, timeout=900)
    resp.raise_for_status()
    return resp.json()


def api_export_docx(content_markdown: str, filename: str) -> bytes:
    url = f"{API_BASE}/export/docx"
    data = {"content": content_markdown, "filename": filename}
    resp = requests.post(url, data=data, timeout=240)
    resp.raise_for_status()
    return resp.content


# --------- CAMADAS DE DADOS UTILIZANDO app.utils.db ---------
def carregar_processos_pendentes_df() -> pd.DataFrame:
    rows = listar_processos(status="pendente")
    if not rows:
        return pd.DataFrame(
            columns=[
                "id",
                "nome_cliente",
                "email",
                "numero_processo",
                "tipo",
                "conferencia",
                "data_envio",
                "caminho_arquivo",
            ]
        )
    df = pd.DataFrame(rows)
    expected_cols = [
        "id",
        "nome_cliente",
        "email",
        "numero_processo",
        "tipo",
        "conferencia",
        "data_envio",
        "caminho_arquivo",
    ]
    for c in expected_cols:
        if c not in df.columns:
            df[c] = None
    return df[expected_cols].sort_values(by="data_envio", ascending=False)


def carregar_processos_finalizados_df() -> pd.DataFrame:
    rows = listar_processos(status="finalizado")
    if not rows:
        return pd.DataFrame(columns=["nome_cliente", "email", "numero_processo", "data_envio", "caminho_arquivo"])
    df = pd.DataFrame(rows)
    cols = ["nome_cliente", "email", "numero_processo", "data_envio", "caminho_arquivo"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols].sort_values(by="data_envio", ascending=False)


def carregar_contagem_processos_mensal_df() -> pd.DataFrame:
    rows = listar_processos(status=None)
    if not rows:
        return pd.DataFrame(columns=["nome_cliente", "email", "mes_ano", "quantidade"])
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


# ========= HELPERS: exportar DataFrame sem quebrar se openpyxl não existir =========
def df_to_excel_or_csv_bytes(df: pd.DataFrame, sheet_name: str, fallback_csv_name: str):
    """
    Tenta gerar XLSX (openpyxl). Se não houver openpyxl, gera CSV.
    Retorna (bytes, filename, mime).
    """
    try:
        import openpyxl  # noqa: F401

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=sheet_name)
        return output.getvalue(), f"{sheet_name}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except Exception:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        return csv_bytes, fallback_csv_name, "text/csv"


# ========= APP STREAMLIT =========
st.set_page_config(page_title="JusReport", page_icon="⚖️", layout="wide")

st.sidebar.title("Navegação")
pagina = st.sidebar.selectbox("Escolha a página", ["Área do Cliente", "Área Jusreport"])

# Avisos de config
if not API_BASE:
    st.sidebar.error("⚠️ JUSREPORT_API_URL não configurada (API_BASE vazio). Configure nos Secrets do Streamlit Cloud.")
else:
    st.sidebar.caption(f"API: {API_BASE}")

if not EMAIL_REMETENTE or not SENHA_APP:
    st.sidebar.info(
        "⚠️ Configure EMAIL_REMETENTE e SENHA_APP (Secrets no Streamlit Cloud / .env local) para enviar e-mails."
    )


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
        conferencia = st.radio(
            "Tipo de relatório desejado:",
            ["Conferido por um advogado", "Sem conferência"],
            index=0,
        )
        arquivo = st.file_uploader("Anexar arquivo do processo (PDF, DOCX)", type=["pdf", "docx"])
        enviado = st.form_submit_button("Enviar processo")

        if enviado:
            if not (nome_cliente and email and numero and arquivo):
                st.warning("Por favor, preencha todos os campos obrigatórios.")
            else:
                try:
                    processo_id = salvar_processo(
                        nome_cliente,
                        email,
                        numero,
                        tipo,
                        arquivo,
                        conferencia,
                    )
                    st.success(f"Processo enviado com sucesso! ID: {processo_id}")
                except Exception as e:
                    st.error(f"Erro ao salvar processo: {e}")
                    with st.expander("📄 Detalhes técnicos (traceback)"):
                        st.code("".join(traceback.format_exception(type(e), e, e.__traceback__)))


# =====================================================================
# ÁREA INTERNA (JUSREPORT / ADVOGADO)
# =====================================================================
elif pagina == "Área Jusreport":
    st.title("Área Interna - JusReport")

    # Saúde da API
    health = api_health()
    with st.expander("🔎 Debug /health da API", expanded=False):
        st.json(health)

    api_reachable = bool(health.get("api_reachable"))
    gemini_ok = bool(health.get("gemini_configured"))

    if not api_reachable:
        st.error(
            f"Não foi possível conectar na API em {API_BASE}. "
            f"Verifique a variável JUSREPORT_API_URL no Streamlit Cloud. "
            f"Detalhe técnico: {health.get('error')}"
        )
    elif not gemini_ok:
        st.error("GEMINI_API_KEY não configurada no servidor da API. Configure no Render e reinicie a API.")

    # Login persistente
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
    df = carregar_processos_pendentes_df()

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

            with col1:
                caminho_cliente = row.get("caminho_arquivo")
                if caminho_cliente and os.path.exists(caminho_cliente):
                    with open(caminho_cliente, "rb") as file:
                        st.download_button(
                            label="Baixar arquivo do cliente",
                            data=file,
                            file_name=os.path.basename(caminho_cliente),
                            mime="application/octet-stream",
                            key=f"download_{row['id']}",
                        )
                else:
                    st.warning("Arquivo original não encontrado no disco.")

            with col2:
                if st.button("Excluir", key=f"excluir_{row['id']}"):
                    try:
                        excluir_processo_e_arquivo(row["id"], row.get("caminho_arquivo"))
                        st.success(f"Processo de {row['nome_cliente']} excluído.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erro ao excluir: {e}")
                        with st.expander("📄 Detalhes técnicos (traceback)"):
                            st.code("".join(traceback.format_exception(type(e), e, e.__traceback__)))

            with col3:
                if (not api_reachable) or (not gemini_ok):
                    st.button("Processar automaticamente", key=f"processar_{row['id']}", disabled=True)
                    st.caption("Ative a API/Gemini para liberar o processamento automático.")
                else:
                    if st.button("Processar automaticamente", key=f"processar_{row['id']}"):
                        try:
                            caminho_cliente = row.get("caminho_arquivo")
                            if not caminho_cliente or not os.path.exists(caminho_cliente):
                                st.error("Arquivo do cliente não encontrado para processar.")
                                st.stop()

                            # 1) Ingest
                            with st.spinner("Iniciando ingestão (upload para API)..."):
                                resp = api_ingest(
                                    file_path=caminho_cliente,
                                    case_number=str(row["numero_processo"]),
                                    client_id=row["email"],
                                )
                            job_id = resp.get("job_id")
                            if not job_id:
                                st.error(f"Falha ao iniciar ingestão: {resp}")
                                st.stop()

                            # 2) Polling de status
                            pbar = st.progress(0)
                            status_area = st.empty()
                            st_status = None

                            while True:
                                time.sleep(1.5)
                                st_status = api_status(job_id)

                                prog = int(st_status.get("progress", 0))
                                detail = st_status.get("detail", "")
                                pbar.progress(min(max(prog, 0), 100))
                                status_area.info(f"Status do índice: {prog}% - {detail}")

                                if st_status.get("status") in ("done", "error"):
                                    break

                            if st_status.get("status") != "done":
                                st.error(f"Ingestão falhou: {st_status.get('detail')}")
                                st.stop()

                            # 3) Sumarização (multiagentes)
                            with st.spinner("Gerando sumarização com IA (multiagentes)..."):
                                query_densa = (
                                    "Gerar relatório completo da execução, contemplando: "
                                    "Cabeçalho (Número dos autos, Classe, Vara, Comarca, Data da distribuição, "
                                    "Exequente, Executados, Advogados, Valor da causa, Valor atualizado, "
                                    "Operação financeira, Número da operação, Valor da operação, Datas, Garantias); "
                                    "Resumo da Inicial (origem da dívida, contrato/confissão de dívida, cheques, multa, penhor mercantil); "
                                    "Tentativas de Penhora Online (RENAJUD, SISBAJUD, INFOJUD, SERASAJUD) e garantias; "
                                    "Movimentações Processuais relevantes em ordem cronológica; "
                                    "Análise Jurídica (partes, advogados, garantias, citações, penhoras, planilhas, defesas, embargos, "
                                    "prescrição, paralisações)."
                                )

                                sum_resp = api_summarize(
                                    question=query_densa,
                                    case_number=str(row["numero_processo"]),
                                    action_type=str(row["tipo"]),
                                    k=100,
                                    return_json=True,
                                )

                            summary_md = (sum_resp.get("summary_markdown", "") or "").strip()
                            if not summary_md:
                                st.error("A IA não retornou conteúdo para o relatório.")
                                st.stop()

                            st.markdown("**Prévia do relatório:**")
                            st.markdown(summary_md)

                            # 4) Export DOCX
                            nome_saida = f"Sum_{row['numero_processo']}.docx"
                            with st.spinner("Exportando relatório para DOCX..."):
                                docx_bytes = api_export_docx(content_markdown=summary_md, filename=nome_saida)

                            caminho_relatorio = os.path.join(RELATORIOS_DIR, nome_saida)
                            with open(caminho_relatorio, "wb") as out:
                                out.write(docx_bytes)

                            if not os.path.exists(caminho_relatorio) or os.path.getsize(caminho_relatorio) == 0:
                                st.error("Arquivo DOCX não foi salvo corretamente.")
                                st.stop()

                            registrar_relatorio(row["id"], caminho_docx=caminho_relatorio)

                            # Envia por e-mail se "Sem conferência"
                            if str(row.get("conferencia", "")).strip().lower().startswith("sem"):
                                finalizar_processo_e_enviar(
                                    row["id"], caminho_relatorio, row["email"], str(row["numero_processo"])
                                )
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

        bytes_out, fname_out, mime_out = df_to_excel_or_csv_bytes(
            df_finalizados.drop(columns=["caminho_arquivo"], errors="ignore"),
            sheet_name="RelatoriosFinalizados",
            fallback_csv_name="relatorios_finalizados.csv",
        )

        st.download_button(
            label=f"Baixar Relatórios Finalizados ({'Excel' if fname_out.endswith('.xlsx') else 'CSV'})",
            data=bytes_out,
            file_name=fname_out,
            mime=mime_out,
        )

    # -------- Relatório Mensal --------
    st.subheader("Relatório Mensal de Processos por Cliente")
    df_contagem = carregar_contagem_processos_mensal_df()

    if df_contagem.empty:
        st.info("Nenhum processo enviado ainda para gerar o relatório.")
    else:
        st.dataframe(df_contagem)

        bytes_out, fname_out, mime_out = df_to_excel_or_csv_bytes(
            df_contagem,
            sheet_name="RelatorioMensal",
            fallback_csv_name="relatorio_mensal_processos.csv",
        )

        st.download_button(
            label=f"Baixar Relatório Mensal ({'Excel' if fname_out.endswith('.xlsx') else 'CSV'})",
            data=bytes_out,
            file_name=fname_out,
            mime=mime_out,
        )
