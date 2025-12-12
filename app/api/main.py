import os
import uuid
import io
import traceback
from typing import Dict, Any, Optional, Tuple, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

import pdfplumber
import google.generativeai as genai

from docx import Document
from docx.shared import Pt, Inches
from pydantic import BaseModel

# pdf2image é opcional – usamos se estiver instalada
try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    print("[AVISO] pdf2image não está instalado. Prints de planilhas não serão gerados.")


# ============================================================
# CONFIGURAÇÃO BÁSICA (PATHS, .ENV, GEMINI, PASTAS)
# ============================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REL_DIR = os.path.join(DATA_DIR, "relatorios")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REL_DIR, exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_TEXT = os.getenv("GEMINI_MODEL_TEXT", "gemini-2.5-pro")

text_model = None
if GEMINI_API_KEY:
    print(f"[INFO] GEMINI_API_KEY detectada (prefixo={GEMINI_API_KEY[:6]}...)")
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        try:
            text_model = genai.GenerativeModel(GEMINI_MODEL_TEXT)
            print(f"[INFO] Carregado modelo Gemini: {GEMINI_MODEL_TEXT}")
        except Exception as e:
            print(f"[AVISO] Falha ao carregar modelo {GEMINI_MODEL_TEXT}: {e}")
            print("[AVISO] Tentando fallback para 'gemini-1.5-pro'...")
            text_model = genai.GenerativeModel("gemini-1.5-pro")
            GEMINI_MODEL_TEXT = "gemini-1.5-pro"
            print(f"[INFO] Fallback bem-sucedido, usando: {GEMINI_MODEL_TEXT}")
    except Exception as e:
        print(f"[ERRO] Falha ao configurar Gemini: {e}")
        text_model = None
else:
    print("[AVISO] GEMINI_API_KEY não configurada. IA desativada na API.")


# ============================================================
# FASTAPI + CORS
# ============================================================

app = FastAPI(title="API Jurídica - JusReport")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: Dict[str, Dict[str, Any]] = {}


# ============================================================
# MODELOS P/ REQUESTS
# ============================================================

class SummarizeRequest(BaseModel):
    question: str
    case_number: str
    action_type: str
    k: int = 50
    return_json: bool = True


class SummarizeTextRequest(BaseModel):
    text: str
    case_number: str
    action_type: str


# ============================================================
# HEALTH (GET + HEAD)
# ============================================================

@app.get("/health")
def health():
    env_val = os.getenv("GEMINI_API_KEY")
    return {
        "service": "api-juridica",
        "gemini_env_present": env_val is not None,
        "gemini_env_prefix": (env_val[:6] + "...") if env_val else None,
        "gemini_configured": bool(env_val),
        "gemini_model": GEMINI_MODEL_TEXT if env_val else None,
    }

@app.head("/health")
def health_head():
    # evita 405 quando alguma plataforma faz HEAD
    return


# ============================================================
# INGEST (opcional — no Cloud pode dar 502, por isso UI usa /summarize_text)
# ============================================================

@app.post("/ingest")
async def ingest(
    files: list[UploadFile] = File(...),
    case_number: str = Form(...),
    client_id: str | None = Form(None),
):
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado")

    f = files[0]
    job_id = str(uuid.uuid4())
    filename = f"{job_id}__{f.filename}"
    save_path = os.path.join(UPLOAD_DIR, filename)

    content = await f.read()

    # proteção simples: se vier gigante, devolve 413 (melhor que 502)
    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "35"))
    if len(content) > max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo muito grande ({len(content)/1024/1024:.1f}MB). Limite atual: {max_upload_mb}MB"
        )

    with open(save_path, "wb") as out:
        out.write(content)

    JOBS[job_id] = {
        "status": "done",
        "progress": 100,
        "detail": "Ingestão concluída",
        "file_path": save_path,
        "case_number": case_number,
        "client_id": client_id,
        "meta": {},
    }

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return {
        "status": job["status"],
        "progress": job["progress"],
        "detail": job.get("detail", ""),
        "result": None,
    }


# ============================================================
# EXTRAÇÃO DE TEXTO DO PDF
# ============================================================

def _detect_planilha_pages(text_by_page: List[str]) -> List[int]:
    keywords = [
        "planilha", "demonstrativo", "cálculo", "calculo",
        "sisbajud", "bacenjud", "bloqueio", "penhora online", "penhora on-line",
    ]
    out = []
    for idx, page_text in enumerate(text_by_page):
        tl = (page_text or "").lower()
        if any(k in tl for k in keywords):
            out.append(idx + 1)
    return out


def _build_global_sample(full_text: str, max_chars: int) -> str:
    total_len = len(full_text)
    if total_len <= max_chars:
        return full_text

    part = max_chars // 4 or max_chars

    inicio = full_text[:part]

    mid_center = total_len // 2
    mid_start = max(0, mid_center - part // 2)
    mid_end = min(total_len, mid_start + part)
    meio = full_text[mid_start:mid_end]

    pre_final_start = max(0, total_len - (part * 2))
    pre_final_end = min(total_len, pre_final_start + part)
    pre_final = full_text[pre_final_start:pre_final_end]

    fim = full_text[-part:]

    return (
        inicio
        + "\n\n=== TRECHO CENTRAL DO PROCESSO ===\n\n" + meio
        + "\n\n=== TRECHO PRÉ-FINAL DO PROCESSO ===\n\n" + pre_final
        + "\n\n=== TRECHO FINAL DO PROCESSO ===\n\n" + fim
    )


def _extract_text_from_pdf(path: str) -> Tuple[str, Dict[str, Any]]:
    text_by_page: List[str] = []
    pages_obj = []

    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                pages_obj.append(page)
                t = page.extract_text() or ""
                text_by_page.append(t)
    except Exception as e:
        print(f"[ERRO] Falha ao ler PDF {path}: {e}")

    full_text = "\n\n".join(text_by_page) if text_by_page else ""
    total_len = len(full_text)

    if total_len == 0:
        return "", {"planilha_pages": []}

    env_max = int(os.getenv("MAX_PDF_CHARS", "30000"))
    HARD_CAP_CHARS = int(os.getenv("HARD_CAP_CHARS", "80000"))
    max_chars = min(env_max, HARD_CAP_CHARS)

    print(f"[INFO] usando max_chars={max_chars} | total_len={total_len}")

    if total_len <= max_chars:
        planilha_pages = _detect_planilha_pages(text_by_page)
        return full_text, {"planilha_pages": planilha_pages}

    keywords = [
        "planilha", "demonstrativo", "cálculo", "calculo",
        "sisbajud", "bacenjud", "bloqueio", "penhora online", "penhora on-line",
    ]

    hotspot_parts: List[str] = []
    planilha_pages: List[int] = []

    for idx, page_text in enumerate(text_by_page):
        raw_page_text = page_text or ""
        tl = raw_page_text.lower()
        if any(k in tl for k in keywords):
            page_num = idx + 1
            planilha_pages.append(page_num)

            bloco = [f"\n\n=== PÁGINA RELEVANTE {page_num} ===\n\n", raw_page_text]

            try:
                page = pages_obj[idx]
                tables = page.extract_tables() or []
            except Exception:
                tables = []

            if tables:
                bloco.append(f"\n\n=== TABELAS EXTRAÍDAS NA PÁGINA {page_num} ===\n\n")
                for t_idx, table in enumerate(tables, start=1):
                    bloco.append(f"--- Tabela {t_idx} ---\n")
                    for row in table:
                        row = [cell if cell is not None else "" for cell in row]
                        bloco.append(" | ".join(row) + "\n")
                    bloco.append("\n")

            hotspot_parts.append("".join(bloco))

    hotspot_text = "".join(hotspot_parts).strip()

    if not hotspot_text:
        global_sample = _build_global_sample(full_text, max_chars)
        return global_sample, {"planilha_pages": []}

    max_hotspot = int(max_chars * 0.6)
    if len(hotspot_text) > max_hotspot:
        hotspot_text = hotspot_text[:max_hotspot]

    remaining_chars = max_chars - len(hotspot_text)
    if remaining_chars <= 0:
        return hotspot_text, {"planilha_pages": planilha_pages}

    global_sample = _build_global_sample(full_text, remaining_chars)

    final_text = hotspot_text + "\n\n=== AMOSTRAGEM GLOBAL DO PROCESSO ===\n\n" + global_sample
    return final_text, {"planilha_pages": planilha_pages}


# ============================================================
# AGENTES (PROMPTS)
# ============================================================

def _run_execucao_agents(base_text: str, case_number: str, action_type: str) -> Tuple[str, dict]:
    if not text_model:
        raise RuntimeError("Modelo Gemini não está configurado (text_model=None).")

    tasks = [
        {
            "key": "cabecalho",
            "title": "Cabeçalho",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.
Extraia o cabeçalho completo: número, classe, vara/comarca, distribuição, partes, advogados, valores, contrato/operação, garantias.
Regras: não inventar; se não tiver, escrever "Não informado"; mencionar fls./páginas quando constar.
Formato: Markdown com "## Cabeçalho" e bullets "• ".
""",
        },
        {
            "key": "resumo_inicial",
            "title": "Resumo da Petição Inicial",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.
Faça um resumo detalhado da inicial: partes, origem da dívida, valores, garantias e pedidos (em bullets).
Não inventar. Formato: Markdown com "## Resumo da Petição Inicial".
""",
        },
        {
            "key": "penhora",
            "title": "Tentativas de Penhora Online e Garantias",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Liste detalhadamente tentativas/decisões/resultados de RENAJUD, SISBAJUD/BACENJUD, INFOJUD, SERASAJUD e outros.
Se não houver nos trechos, dizer "Não há informação nos trechos analisados...".
""",
        },
        {
            "key": "valores_planilhas",
            "title": "Valores e Planilhas de Débito",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Minere valores, planilhas/demonstrativos, datas de atualização e bloqueios efetivos.
Monte tabela de evolução se houver mais de uma planilha.
Não inventar.
""",
        },
        {
            "key": "movimentacoes",
            "title": "Movimentações Processuais Relevantes",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Crie uma linha do tempo detalhada com atos relevantes e datas (ou ano aproximado).
Não opinar, só descrever.
""",
        },
        {
            "key": "analise_juridica",
            "title": "Análise Jurídica",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Organize bullets consolidados (partes, advogados, garantias, citações, penhoras, planilhas, defesas, prescrição, paralisações).
Se não tiver dado no texto: "Não informado".
""",
        },
    ]

    sections: dict[str, str] = {}
    for task in tasks:
        prompt = f"""{task["instruction"]}

=== TEXTO DO PROCESSO (EXTRAÍDO) ===
\"\"\"{base_text}\"\"\"
"""
        print(f"[AGENTE] Rodando: {task['key']} ({task['title']})")
        resp = text_model.generate_content(prompt)
        sections[task["key"]] = (resp.text or "").strip()

    md_parts: List[str] = []
    md_parts.append(f"Sumarização da {action_type} ({case_number})\n")

    order = ["cabecalho", "resumo_inicial", "penhora", "valores_planilhas", "movimentacoes", "analise_juridica"]
    for key in order:
        section_text = (sections.get(key) or "").strip()
        if not section_text:
            title = next(t["title"] for t in tasks if t["key"] == key)
            md_parts.append(f"## {title}\n\nNão informado.")
        else:
            md_parts.append(section_text)

    final_md = "\n\n".join(md_parts)
    return final_md, sections


# ============================================================
# SUMMARIZE (via JOBS + PDF no servidor)
# ============================================================

@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    try:
        case_number = req.case_number
        action_type = req.action_type

        job = None
        for j in JOBS.values():
            if j.get("case_number") == case_number:
                job = j
                break

        if not job:
            raise HTTPException(status_code=404, detail="Nenhum job encontrado para esse número de processo")

        file_path = job["file_path"]

        if not GEMINI_API_KEY or not text_model:
            raise HTTPException(status_code=500, detail="Gemini não configurado na API")

        base_text, meta = _extract_text_from_pdf(file_path)
        if not base_text:
            base_text = "Não foi possível extrair texto do PDF."

        job_meta = job.get("meta") or {}
        job_meta.update(meta or {})
        job["meta"] = job_meta

        final_md, sections = _run_execucao_agents(base_text, case_number, action_type)

        return {
            "summary_markdown": final_md,
            "sections": sections,
            "used_chunks": [],
            "result": {"meta": meta},
        }

    except HTTPException:
        raise
    except Exception as e:
        print("ERRO EM /summarize:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")


# ============================================================
# SUMMARIZE_TEXT (recomendado no Cloud: sem upload)
# ============================================================

@app.post("/summarize_text")
async def summarize_text(req: SummarizeTextRequest):
    try:
        if not GEMINI_API_KEY or not text_model:
            raise HTTPException(status_code=500, detail="Gemini não configurado na API")

        base_text = (req.text or "").strip()
        if not base_text:
            raise HTTPException(status_code=400, detail="Texto vazio recebido em /summarize_text")

        final_md, sections = _run_execucao_agents(base_text, req.case_number, req.action_type)

        return {
            "summary_markdown": final_md,
            "sections": sections,
            "used_chunks": [],
            "result": {"meta": {"source": "streamlit_text"}},
        }

    except HTTPException:
        raise
    except Exception as e:
        print("ERRO EM /summarize_text:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")


# ============================================================
# EXPORT DOCX
# ============================================================

@app.post("/export/docx")
async def export_docx(
    content: str = Form(...),
    filename: str = Form("relatorio.docx"),
    case_number: str | None = Form(None),
    include_planilha_images: bool = Form(False),
):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    for line in content.splitlines():
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        else:
            doc.add_paragraph(line)

    if include_planilha_images and case_number:
        if PDF2IMAGE_AVAILABLE:
            job = None
            for j in JOBS.values():
                if j.get("case_number") == case_number:
                    job = j
                    break
            if job:
                meta = job.get("meta") or {}
                planilha_pages = sorted(set(meta.get("planilha_pages") or []))
                file_path = job.get("file_path")
                if planilha_pages and file_path and os.path.exists(file_path):
                    try:
                        images = convert_from_path(file_path)
                        doc.add_page_break()
                        doc.add_heading("Anexos – Planilhas e Bloqueios Relevantes", level=1)
                        for p in planilha_pages:
                            if 1 <= p <= len(images):
                                img = images[p - 1]
                                img_bytes = io.BytesIO()
                                img.save(img_bytes, format="PNG")
                                img_bytes.seek(0)
                                doc.add_paragraph(f"Planilha / demonstrativo – pág. {p}")
                                doc.add_picture(img_bytes, width=Inches(6.0))
                                doc.add_paragraph("")
                    except Exception as e:
                        print(f"[AVISO] Falha ao inserir imagens: {e}")

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
