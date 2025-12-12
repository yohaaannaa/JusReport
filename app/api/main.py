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

# BASE_DIR = raiz do projeto (pasta JusReport)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

# Carrega .env apenas se existir (local). No Render, o ideal é usar Environment variables.
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)
    print(f"[INFO] .env carregado de: {env_path}")
else:
    print("[INFO] .env não encontrado (ok em produção). Usando variáveis do ambiente.")

# Pastas de dados
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REL_DIR = os.path.join(DATA_DIR, "relatorios")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REL_DIR, exist_ok=True)

# Config Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_TEXT = os.getenv("GEMINI_MODEL_TEXT", "gemini-2.5-pro")

text_model = None

def _configure_gemini() -> None:
    """Configura o Gemini e instancia o modelo com fallback."""
    global text_model, GEMINI_MODEL_TEXT

    if not GEMINI_API_KEY:
        print("[AVISO] GEMINI_API_KEY não configurada. IA desativada na API.")
        text_model = None
        return

    print(f"[INFO] GEMINI_API_KEY detectada (prefixo={GEMINI_API_KEY[:6]}...)")
    genai.configure(api_key=GEMINI_API_KEY)

    # tenta modelo principal
    try:
        text_model = genai.GenerativeModel(GEMINI_MODEL_TEXT)
        print(f"[INFO] Carregado modelo Gemini: {GEMINI_MODEL_TEXT}")
        return
    except Exception as e:
        print(f"[AVISO] Falha ao carregar modelo {GEMINI_MODEL_TEXT}: {e}")

    # fallback
    try:
        print("[AVISO] Tentando fallback para 'gemini-1.5-pro'...")
        text_model = genai.GenerativeModel("gemini-1.5-pro")
        GEMINI_MODEL_TEXT = "gemini-1.5-pro"
        print(f"[INFO] Fallback bem-sucedido, usando: {GEMINI_MODEL_TEXT}")
    except Exception as e:
        print(f"[ERRO] Falha no fallback do Gemini: {e}")
        text_model = None


_configure_gemini()


# ============================================================
# FASTAPI + CORS
# ============================================================

app = FastAPI(title="API Jurídica - JusReport")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # simplificado para ambiente local / nuvem
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# "Banco" simplificado em memória para monitorar ingest
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
def health():
    """
    Usado pela interface Streamlit para verificar se a API está viva
    e se o Gemini foi configurado.
    """
    env_val = os.getenv("GEMINI_API_KEY")
    return {
        "service": "api-juridica",
        "gemini_env_present": env_val is not None,
        "gemini_env_prefix": (env_val[:6] + "...") if env_val else None,
        "gemini_configured": bool(env_val) and (text_model is not None),
        "gemini_model": GEMINI_MODEL_TEXT if env_val else None,
    }

# opcional: evitar 405 em HEAD /health (alguns health-checkers usam HEAD)
@app.head("/health")
def health_head():
    return JSONResponse(content={}, status_code=200)


@app.post("/ingest")
async def ingest(
    files: list[UploadFile] = File(...),
    case_number: str = Form(...),
    client_id: str | None = Form(None),
):
    """
    Recebe 1 arquivo (usaremos só o primeiro por enquanto),
    salva em data/uploads e cria um job já marcado como "done"
    (ingestão simplificada).
    """
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado")

    f = files[0]
    job_id = str(uuid.uuid4())
    filename = f"{job_id}__{f.filename}"
    save_path = os.path.join(UPLOAD_DIR, filename)

    content = await f.read()
    with open(save_path, "wb") as out:
        out.write(content)

    JOBS[job_id] = {
        "status": "done",
        "progress": 100,
        "detail": "Ingestão concluída (simples)",
        "file_path": save_path,
        "case_number": case_number,
        "client_id": client_id,
        "meta": {},
    }

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    """
    Usado pela interface para exibir barra de progresso.
    Aqui o job já fica como 100% concluído.
    """
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

def _detect_planilha_pages(text_by_page: list[str]) -> list[int]:
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
    planilha_pages: list[int] = []
    for idx, page_text in enumerate(text_by_page):
        tl = (page_text or "").lower()
        if any(k in tl for k in keywords):
            planilha_pages.append(idx + 1)
    return planilha_pages


def _build_global_sample(full_text: str, max_chars: int) -> str:
    total_len = len(full_text)
    if total_len <= max_chars:
        return full_text

    part = max_chars // 4
    if part == 0:
        part = max_chars

    inicio = full_text[:part]

    mid_center = total_len // 2
    mid_start = max(0, mid_center - part // 2)
    mid_end = min(total_len, mid_start + part)
    meio = full_text[mid_start:mid_end]

    pre_final_start = max(0, total_len - (part * 2))
    pre_final_end = pre_final_start + part
    if pre_final_end > total_len:
        pre_final_end = total_len
        pre_final_start = max(0, pre_final_end - part)
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


def _extract_text_from_pdf(path: str) -> tuple[str, Dict[str, Any]]:
    text_by_page: list[str] = []
    pages_obj = []

    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                pages_obj.append(page)
                t = page.extract_text() or ""
                text_by_page.append(t)
    except Exception as e:
        print(f"[ERRO] Falha ao ler PDF {path}: {e}")
        return "", {"planilha_pages": []}

    full_text = "\n\n".join(text_by_page) if text_by_page else ""
    total_len = len(full_text)

    if total_len == 0:
        return "", {"planilha_pages": []}

    env_max = int(os.getenv("MAX_PDF_CHARS", "30000"))

    # Em cloud, seja conservadora para evitar resposta vazia/timeout
    HARD_CAP_CHARS = int(os.getenv("HARD_CAP_CHARS", "80000"))
    max_chars = min(env_max, HARD_CAP_CHARS)

    print(
        f"[INFO] MAX_PDF_CHARS={env_max} | HARD_CAP_CHARS={HARD_CAP_CHARS} "
        f"| usando max_chars={max_chars} | total_len={total_len}"
    )

    if total_len <= max_chars:
        planilha_pages = _detect_planilha_pages(text_by_page)
        return full_text, {"planilha_pages": planilha_pages}

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

    hotspot_parts: list[str] = []
    planilha_pages: list[int] = []

    for idx, page_text in enumerate(text_by_page):
        raw_page_text = page_text or ""
        tl = raw_page_text.lower()

        if any(k in tl for k in keywords):
            page_num = idx + 1
            planilha_pages.append(page_num)

            bloco = [f"\n\n=== PÁGINA RELEVANTE {page_num} (palavras-chave localizadas) ===\n\n"]
            bloco.append(raw_page_text)

            try:
                page = pages_obj[idx]
                tables = page.extract_tables() or []
            except Exception as te:
                print(f"[AVISO] Falha ao extrair tabelas da página {page_num}: {te}")
                tables = []

            if tables:
                bloco.append(f"\n\n=== CONTEÚDO DA PLANILHA DETECTADA NA PÁGINA {page_num} ===\n\n")
                for t_idx, table in enumerate(tables, start=1):
                    bloco.append(f"--- Tabela {t_idx} (pág. {page_num}) ---\n")
                    for row in table:
                        row = [cell if cell is not None else "" for cell in row]
                        linha = " | ".join(row)
                        bloco.append(linha + "\n")
                    bloco.append("\n")

            hotspot_parts.append("".join(bloco))

    hotspot_text = "".join(hotspot_parts).strip()

    if not hotspot_text:
        global_sample = _build_global_sample(full_text, max_chars)
        return global_sample, {"planilha_pages": []}

    max_hotspot = int(max_chars * 0.6)
    if len(hotspot_text) > max_hotspot:
        print(f"[AVISO] Hotspots muito grandes ({len(hotspot_text)}). Truncando para {max_hotspot}.")
        hotspot_text = hotspot_text[:max_hotspot]

    remaining_chars = max_chars - len(hotspot_text)
    if remaining_chars <= 0:
        return hotspot_text, {"planilha_pages": planilha_pages}

    global_sample = _build_global_sample(full_text, remaining_chars)

    final_text = (
        hotspot_text
        + "\n\n=== AMOSTRAGEM GLOBAL DO PROCESSO ===\n\n"
        + global_sample
    )

    print(
        f"[INFO] Texto final len={len(final_text)} (máx={max_chars}), "
        f"hotspots_len={len(hotspot_text)}, global_len={len(global_sample)}"
    )
    return final_text, {"planilha_pages": planilha_pages}


# ============================================================
# "AGENTES" DE EXECUÇÃO: VÁRIAS PERGUNTAS → UM RELATÓRIO
# ============================================================

def _safe_generate_content(prompt: str, section_title: str) -> str:
    """
    Chama o Gemini e falha explicitamente se vier vazio.
    Isso impede a UI de receber relatório vazio.
    """
    if not text_model:
        raise RuntimeError("Modelo Gemini não está configurado (text_model=None).")

    try:
        resp = text_model.generate_content(prompt)
    except Exception as e:
        raise RuntimeError(f"Falha ao chamar Gemini na seção '{section_title}': {e}")

    raw_text = getattr(resp, "text", None)

    if not raw_text or not raw_text.strip():
        raise RuntimeError(
            f"Gemini retornou resposta vazia na seção '{section_title}'. "
            f"Possíveis causas: prompt grande demais, timeout, bloqueio ou instabilidade."
        )

    return raw_text.strip()


def _run_execucao_agents(base_text: str, case_number: str, action_type: str) -> tuple[str, dict]:
    tasks = [
        {
            "key": "cabecalho",
            "title": "Cabeçalho",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.

Sua tarefa NÃO é resumir, mas sim **organizar todas as informações relevantes** que encontrar.

Com base EXCLUSIVAMENTE no texto abaixo, extraia o CABEÇALHO do processo, contendo, de forma
o mais completa possível:

• Número dos autos  
• Classe da ação  
• Vara Cível e Comarca responsável (com número da Vara, se houver)  
• Data da distribuição da ação (se tiver mais de uma, liste todas com contexto)  
• Exequente (nome completo e, se constar, CNPJ/CPF)  
• Executados (nomes completos e, se constar, CNPJ/CPF, listados separadamente)  
• Advogados do Exequente (nome, OAB, UF, listar todos os que aparecerem)  
• Advogados dos Executados (nome, OAB, UF, listar todos os que aparecerem)  
• Valor da causa (com data de referência, se constar)  
• Valor executado/atualizado na inicial (valor + data de referência, se constar)  
• Operação financeira que originou a ação (ex.: Cédula de Crédito Bancário, confissão de dívida etc.)  
• Número da operação (se constar)  
• Valor original da operação de crédito  
• Datas relevantes da operação (emissão, vencimentos, eventual renegociação)  
• Garantias oferecidas na operação (penhor, hipoteca, fiança, aval etc.) com breve descrição.

REGRAS:
- Se algum item não aparecer, escreva "Não informado".
- Sempre que houver fls./páginas, mencione entre parênteses.
- Responda em Markdown começando com "## Cabeçalho" e bullets iniciando com "• ".
"""
        },
        {
            "key": "resumo_inicial",
            "title": "Resumo da Petição Inicial",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.

Com base EXCLUSIVAMENTE no texto abaixo, elabore o **RESUMO DA PETIÇÃO INICIAL** com riqueza de detalhes:
- Partes (exequente x executados; fiadores/avalistas se houver)
- Origem da dívida (tipo de contrato, número, data, valor, condições)
- Valores (valor executado + data; encargos se constarem)
- Garantias (quais e breve descrição)
- Pedidos (em bullets)

Comece com "## Resumo da Petição Inicial".
"""
        },
        {
            "key": "penhora",
            "title": "Tentativas de Penhora Online e Garantias",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Com base EXCLUSIVAMENTE no texto abaixo, faça "## Tentativas de Penhora Online e Garantias":
- SISBAJUD/BACENJUD, RENAJUD, INFOJUD, SERASAJUD (pedido/decisão/resultado/datas/valores)
- constrições (imóveis, móveis, arrestos, registros)
- se não houver informação nos trechos analisados, diga isso explicitamente (sem concluir sobre o processo inteiro)
"""
        },
        {
            "key": "valores_planilhas",
            "title": "Valores e Planilhas de Débito",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Monte "## Valores e Planilhas de Débito":
- valor original da operação
- valor executado na inicial
- planilhas/demonstrativos (data referência, valor total, fls.)
- tabela de evolução (mesmo que só 1 planilha)
- bloqueios efetivos SISBAJUD (data, valor, desfecho)

Se não localizar planilhas posteriores, escreva isso.
"""
        },
        {
            "key": "movimentacoes",
            "title": "Movimentações Processuais Relevantes",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Monte "## Movimentações Processuais Relevantes" em linha do tempo (bullets):
• dd/mm/aaaa: ato relevante (fls. se houver)

Liste o máximo possível de atos relevantes.
"""
        },
        {
            "key": "analise_juridica",
            "title": "Análise Jurídica",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Crie "## Análise Jurídica" com bullets factuais.
Se não houver dado, escreva exatamente "Não informado".
"""
        },
    ]

    sections: dict[str, str] = {}

    for task in tasks:
        prompt = f"""{task["instruction"]}

=== TEXTO DO PROCESSO (EXTRAÍDO DO PDF) ===

\"\"\"{base_text}\"\"\""""
        print(f"[AGENTE] Rodando sub-tarefa: {task['key']} ({task['title']})")
        text = _safe_generate_content(prompt, task["title"])
        sections[task["key"]] = text

    md_parts: list[str] = []
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

    if not final_md or not final_md.strip():
        raise RuntimeError("Gemini não retornou conteúdo válido para o relatório final.")

    return final_md, sections


# ============================================================
# ENDPOINT /summarize
# ============================================================

@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    try:
        case_number = req.case_number
        action_type = req.action_type

        # achar job
        job = None
        for j in JOBS.values():
            if j.get("case_number") == case_number:
                job = j
                break

        if not job:
            raise HTTPException(status_code=404, detail="Nenhum job encontrado para esse número de processo")

        file_path = job["file_path"]

        if not GEMINI_API_KEY or not text_model:
            raise HTTPException(status_code=500, detail="Gemini não configurado na API (Render env / .env local)")

        base_text, meta = _extract_text_from_pdf(file_path)
        if not base_text.strip():
            raise HTTPException(status_code=400, detail="Não foi possível extrair texto do PDF (texto vazio).")

        if meta:
            job_meta = job.get("meta") or {}
            job_meta.update(meta)
            job["meta"] = job_meta

        final_md, sections = _run_execucao_agents(
            base_text=base_text,
            case_number=case_number,
            action_type=action_type,
        )

        # proteção final (nunca devolve vazio)
        if not final_md or not final_md.strip():
            raise HTTPException(status_code=500, detail="Gemini não retornou conteúdo válido para o relatório.")

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
# ENDPOINT /export/docx
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
        if not PDF2IMAGE_AVAILABLE:
            print("[AVISO] include_planilha_images=True, mas pdf2image não está disponível.")
        else:
            job = None
            for j in JOBS.values():
                if j.get("case_number") == case_number:
                    job = j
                    break

            if job:
                meta = job.get("meta") or {}
                planilha_pages = sorted(set(meta.get("planilha_pages") or []))

                if planilha_pages:
                    file_path = job.get("file_path")
                    if file_path and os.path.exists(file_path):
                        try:
                            print(f"[INFO] Gerando imagens (páginas {planilha_pages}) para anexar no DOCX...")
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
                            print(f"[AVISO] Falha ao gerar/inserir imagens de planilha no DOCX: {e}")
                    else:
                        print("[AVISO] Caminho do PDF não encontrado ao tentar gerar imagens.")
                else:
                    print("[INFO] Nenhuma página marcada em meta['planilha_pages'].")
            else:
                print("[AVISO] Nenhum job encontrado para esse case_number no /export/docx.")

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
