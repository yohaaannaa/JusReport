import os
import uuid
import io
import traceback
from typing import Dict, Any, Optional, Tuple, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from dotenv import load_dotenv
import pdfplumber
import google.generativeai as genai
from pydantic import BaseModel
from docx import Document
from docx.shared import Pt, Inches

# pdf2image é opcional – usamos se estiver instalada
try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except Exception:
    PDF2IMAGE_AVAILABLE = False
    print("[AVISO] pdf2image não está instalado. Prints de planilhas não serão gerados.")


# ============================================================
# CONFIGURAÇÃO BÁSICA (PATHS, .ENV, GEMINI, PASTAS)
# ============================================================

# BASE_DIR = raiz do projeto (pasta JusReport)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))  # local; no Render, env vars já vêm do dashboard

# Pastas de dados
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REL_DIR = os.path.join(DATA_DIR, "relatorios")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REL_DIR, exist_ok=True)

# Limites
# Render Free costuma morrer com PDF grande + extração pesada.
# Padrão: 35MB (ajuste no Render: MAX_UPLOAD_MB=35 ou 50 etc)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "35"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Limite de caracteres para texto do PDF enviado ao modelo
ENV_MAX_PDF_CHARS = int(os.getenv("MAX_PDF_CHARS", "120000"))
HARD_CAP_CHARS = int(os.getenv("HARD_CAP_CHARS", "120000"))  # você quer 120k; deixe igual
EFFECTIVE_MAX_CHARS = min(ENV_MAX_PDF_CHARS, HARD_CAP_CHARS)

# Config Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL_TEXT = os.getenv("GEMINI_MODEL_TEXT", "gemini-2.5-pro").strip()

if GEMINI_API_KEY:
    print(f"[INFO] GEMINI_API_KEY detectada (prefixo={GEMINI_API_KEY[:6]}...)")
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[ERRO] Falha ao configurar Gemini: {e}")
else:
    print("[AVISO] GEMINI_API_KEY não configurada. IA desativada na API.")

# Carrega modelo com fallback
text_model = None
if GEMINI_API_KEY:
    try:
        text_model = genai.GenerativeModel(GEMINI_MODEL_TEXT)
        print(f"[INFO] Carregado modelo Gemini: {GEMINI_MODEL_TEXT}")
    except Exception as e:
        print(f"[AVISO] Falha ao carregar modelo {GEMINI_MODEL_TEXT}: {e}")
        try:
            print("[AVISO] Tentando fallback para 'gemini-1.5-pro'...")
            text_model = genai.GenerativeModel("gemini-1.5-pro")
            GEMINI_MODEL_TEXT = "gemini-1.5-pro"
            print(f"[INFO] Fallback bem-sucedido, usando: {GEMINI_MODEL_TEXT}")
        except Exception as e2:
            print(f"[ERRO] Falha também no fallback: {e2}")
            text_model = None


# ============================================================
# FASTAPI + CORS
# ============================================================

app = FastAPI(title="API Jurídica - JusReport")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ok para protótipo; em produção, restrinja seu domínio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# "Banco" simplificado em memória (no Render reinicia e perde isso — esperado)
JOBS: Dict[str, Dict[str, Any]] = {}


# ============================================================
# MODELO P/ CORPO DO /summarize (JSON)
# ============================================================

class SummarizeRequest(BaseModel):
    question: str
    case_number: str
    action_type: str
    k: int = 50
    return_json: bool = True


# ============================================================
# ENDPOINTS BÁSICOS
# ============================================================

@app.get("/health")
def health_get():
    env_val = os.getenv("GEMINI_API_KEY")
    return {
        "service": "api-juridica",
        "gemini_env_present": env_val is not None and env_val.strip() != "",
        "gemini_env_prefix": (env_val[:6] + "...") if env_val else None,
        "gemini_configured": bool(env_val and env_val.strip()),
        "gemini_model": GEMINI_MODEL_TEXT if (env_val and env_val.strip()) else None,
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_pdf_chars": EFFECTIVE_MAX_CHARS,
    }

@app.head("/health")
def health_head():
    # Para healthchecks que usam HEAD (Render/monitors)
    return JSONResponse(content=None, status_code=200)


@app.post("/ingest")
async def ingest(
    files: list[UploadFile] = File(...),
    case_number: str = Form(...),
    client_id: Optional[str] = Form(None),
):
    """
    Recebe arquivo, salva em disco SEM carregar tudo na RAM (streaming),
    aplica limite de tamanho (MAX_UPLOAD_MB) e cria job em memória.
    """
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado")

    f = files[0]
    job_id = str(uuid.uuid4())

    # Normaliza nome para evitar path traversal
    original_name = os.path.basename(f.filename or "arquivo.pdf")
    filename = f"{job_id}__{original_name}"
    save_path = os.path.join(UPLOAD_DIR, filename)

    # Streaming com limite
    total = 0
    try:
        with open(save_path, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    try:
                        out.close()
                    except Exception:
                        pass
                    try:
                        if os.path.exists(save_path):
                            os.remove(save_path)
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"Arquivo muito grande ({total/1024/1024:.1f}MB). Limite atual: {MAX_UPLOAD_MB}MB"
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Falha ao salvar upload: {e}")

    JOBS[job_id] = {
        "status": "done",
        "progress": 100,
        "detail": f"Ingestão concluída ({total/1024/1024:.1f}MB)",
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
# EXTRAÇÃO DE TEXTO DO PDF (HOTSPOTS + AMOSTRAGEM GLOBAL)
# ============================================================

def _detect_planilha_pages(text_by_page: List[str]) -> List[int]:
    keywords = [
        "planilha",
        "demonstrativo",
        "cálculo",
        "calculo",
        "sisbajud",
        "bacenjud",
        "bloqueio",
        "penhora online",
        "penhora on-line",
    ]
    pages = []
    for idx, page_text in enumerate(text_by_page):
        tl = (page_text or "").lower()
        if any(k in tl for k in keywords):
            pages.append(idx + 1)
    return pages


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
        + "\n\n=== TRECHO CENTRAL DO PROCESSO ===\n\n"
        + meio
        + "\n\n=== TRECHO PRÉ-FINAL DO PROCESSO ===\n\n"
        + pre_final
        + "\n\n=== TRECHO FINAL DO PROCESSO ===\n\n"
        + fim
    )


def _extract_text_from_pdf(path: str) -> Tuple[str, Dict[str, Any]]:
    """
    Extração mais "leve" para Render:
    - 1ª passada: só extrai texto por página (lista de strings)
    - identifica hotspots
    - 2ª passada: só nas páginas hotspot tenta extrair tabelas (sem manter pages_obj em memória)
    """
    text_by_page: List[str] = []

    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text_by_page.append(page.extract_text() or "")
    except Exception as e:
        print(f"[ERRO] Falha ao ler PDF {path}: {e}")
        return "", {"planilha_pages": []}

    full_text = "\n\n".join(text_by_page)
    total_len = len(full_text)
    if total_len == 0:
        return "", {"planilha_pages": []}

    max_chars = EFFECTIVE_MAX_CHARS
    print(f"[INFO] usando max_chars={max_chars} | total_len={total_len}")

    # Se coube tudo
    if total_len <= max_chars:
        planilha_pages = _detect_planilha_pages(text_by_page)
        return full_text, {"planilha_pages": planilha_pages}

    # Hotspots
    keywords = [
        "planilha",
        "demonstrativo",
        "cálculo",
        "calculo",
        "sisbajud",
        "bacenjud",
        "bloqueio",
        "penhora online",
        "penhora on-line",
    ]

    hotspot_pages_idx = []
    for idx, page_text in enumerate(text_by_page):
        tl = (page_text or "").lower()
        if any(k in tl for k in keywords):
            hotspot_pages_idx.append(idx)

    planilha_pages = [i + 1 for i in hotspot_pages_idx]
    hotspot_parts: List[str] = []

    # Texto das páginas hotspot (sem tabelas ainda)
    for idx in hotspot_pages_idx:
        page_num = idx + 1
        bloco = [f"\n\n=== PÁGINA RELEVANTE {page_num} (palavras-chave localizadas) ===\n\n"]
        bloco.append(text_by_page[idx] or "")
        hotspot_parts.append("".join(bloco))

    hotspot_text = "".join(hotspot_parts).strip()

    # Tenta extrair tabelas só nas páginas hotspot (2ª passada)
    if hotspot_pages_idx:
        try:
            with pdfplumber.open(path) as pdf:
                for idx in hotspot_pages_idx[:30]:  # guarda-chuva pra não explodir memória/tempo
                    page_num = idx + 1
                    try:
                        page = pdf.pages[idx]
                        tables = page.extract_tables() or []
                    except Exception as te:
                        print(f"[AVISO] Falha ao extrair tabelas da pág {page_num}: {te}")
                        tables = []

                    if tables:
                        hotspot_text += f"\n\n=== CONTEÚDO DA PLANILHA DETECTADA NA PÁGINA {page_num} ===\n\n"
                        for t_idx, table in enumerate(tables, start=1):
                            hotspot_text += f"--- Tabela {t_idx} (pág. {page_num}) ---\n"
                            for row in table:
                                row = [cell if cell is not None else "" for cell in row]
                                hotspot_text += " | ".join(row) + "\n"
                            hotspot_text += "\n"
        except Exception as e:
            print(f"[AVISO] Falha na 2ª passada (tabelas): {e}")

    if not hotspot_text:
        global_sample = _build_global_sample(full_text, max_chars)
        return global_sample, {"planilha_pages": []}

    # Reserva 60% para hotspots e o resto para amostragem global
    max_hotspot = int(max_chars * 0.6)
    if len(hotspot_text) > max_hotspot:
        hotspot_text = hotspot_text[:max_hotspot]

    remaining = max_chars - len(hotspot_text)
    if remaining <= 0:
        return hotspot_text, {"planilha_pages": planilha_pages}

    global_sample = _build_global_sample(full_text, remaining)

    final_text = (
        hotspot_text
        + "\n\n=== AMOSTRAGEM GLOBAL DO PROCESSO ===\n\n"
        + global_sample
    )

    return final_text, {"planilha_pages": planilha_pages}


# ============================================================
# "AGENTES" (multi chamadas ao Gemini)
# ============================================================

def _gemini_generate(prompt: str) -> str:
    if not text_model:
        raise RuntimeError("Gemini não configurado (text_model=None).")

    try:
        resp = text_model.generate_content(prompt)
        txt = (resp.text or "").strip()
        return txt
    except Exception as e:
        # Aqui cai inclusive PermissionDenied / key leaked / etc.
        raise RuntimeError(f"GeminiError: {e}")


def _run_execucao_agents(base_text: str, case_number: str, action_type: str) -> Tuple[str, dict]:
    tasks = [
        {
            "key": "cabecalho",
            "title": "Cabeçalho",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.
Sua tarefa NÃO é resumir, mas sim organizar todas as informações relevantes que encontrar.
Responda em Markdown começando com "## Cabeçalho" e bullets iniciando com "• ".
Se algum item não aparecer, escreva "Não informado".
""",
        },
        {
            "key": "resumo_inicial",
            "title": "Resumo da Petição Inicial",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.
Faça um resumo rico em detalhes, não superficial.
Comece com o título "## Resumo da Petição Inicial" em Markdown.
""",
        },
        {
            "key": "penhora",
            "title": "Tentativas de Penhora Online e Garantias",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Crie a seção "## Tentativas de Penhora Online e Garantias" com bullets e datas/valores quando houver.
Se não houver informação nos trechos analisados sobre um sistema, diga isso explicitamente.
""",
        },
        {
            "key": "valores_planilhas",
            "title": "Valores e Planilhas de Débito",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Crie a seção "## Valores e Planilhas de Débito" e inclua tabela de evolução se houver mais de uma planilha.
Se não localizar planilhas posteriores, escreva explicitamente isso.
""",
        },
        {
            "key": "movimentacoes",
            "title": "Movimentações Processuais Relevantes",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Monte uma linha do tempo detalhada em bullets:
• dd/mm/aaaa: descrição objetiva do ato (mencione fls. se constar).
""",
        },
        {
            "key": "analise_juridica",
            "title": "Análise Jurídica",
            "instruction": """
Você é um assistente jurídico especialista em EXECUÇÃO.
Crie a seção "## Análise Jurídica" em bullets, factual, sem opinião.
Se não encontrar um item, escreva exatamente "Não informado".
""",
        },
    ]

    sections: dict[str, str] = {}

    for task in tasks:
        prompt = f"""{task["instruction"]}

=== PROCESSO ({action_type}) | Nº {case_number} ===

\"\"\"{base_text}\"\"\"
"""
        print(f"[AGENTE] Rodando: {task['key']} ({task['title']})")
        sections[task["key"]] = _gemini_generate(prompt)

    md_parts: List[str] = [f"Sumarização da {action_type} ({case_number})\n"]

    order = ["cabecalho", "resumo_inicial", "penhora", "valores_planilhas", "movimentacoes", "analise_juridica"]
    for key in order:
        txt = (sections.get(key) or "").strip()
        if not txt:
            title = next(t["title"] for t in tasks if t["key"] == key)
            md_parts.append(f"## {title}\n\nNão informado.")
        else:
            md_parts.append(txt)

    return "\n\n".join(md_parts), sections


# ============================================================
# /summarize
# ============================================================

@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    try:
        case_number = req.case_number
        action_type = req.action_type

        # Localiza job
        job = None
        for j in JOBS.values():
            if j.get("case_number") == case_number:
                job = j
                break

        if not job:
            raise HTTPException(status_code=404, detail="Nenhum job encontrado para esse número de processo")

        file_path = job.get("file_path")
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Arquivo do job não encontrado no servidor")

        if not GEMINI_API_KEY or not text_model:
            raise HTTPException(status_code=500, detail="Gemini não configurado na API (env vars)")

        base_text, meta = _extract_text_from_pdf(file_path)
        if not base_text:
            raise HTTPException(status_code=400, detail="Não foi possível extrair texto do PDF")

        # Salva meta no job
        job_meta = job.get("meta") or {}
        job_meta.update(meta or {})
        job["meta"] = job_meta

        final_md, sections = _run_execucao_agents(base_text, case_number, action_type)

        # Se o Gemini retornou vazio (evita “A IA não retornou conteúdo”)
        if not (final_md or "").strip():
            raise HTTPException(status_code=502, detail="Gemini retornou vazio (sem conteúdo)")

        return {
            "summary_markdown": final_md,
            "sections": sections,
            "used_chunks": [],
            "result": {"meta": meta},
        }

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print("ERRO EM /summarize:\n", tb)
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")


# ============================================================
# /export/docx
# ============================================================

@app.post("/export/docx")
async def export_docx(
    content: str = Form(...),
    filename: str = Form("relatorio.docx"),
    case_number: Optional[str] = Form(None),
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

    # anexos de planilha (opcional)
    if include_planilha_images and case_number and PDF2IMAGE_AVAILABLE:
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
                    print(f"[INFO] Gerando imagens das páginas {planilha_pages} para anexar no DOCX...")
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
                    print(f"[AVISO] Falha ao anexar imagens no DOCX: {e}")

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
