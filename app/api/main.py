import os
import uuid
import io
import traceback
from typing import Dict, Any

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

# BASE_DIR = raiz do projeto (pasta JusReport)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Pastas de dados
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REL_DIR = os.path.join(DATA_DIR, "relatorios")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REL_DIR, exist_ok=True)

# Config Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    print(f"[INFO] GEMINI_API_KEY detectada (prefixo={GEMINI_API_KEY[:6]}...)")
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("[AVISO] GEMINI_API_KEY não configurada. IA desativada na API.")

# Modelo padrão (2.5 Pro, com fallback para 1.5 Pro se necessário)
GEMINI_MODEL_TEXT = os.getenv("GEMINI_MODEL_TEXT", "gemini-2.5-pro")

if GEMINI_API_KEY:
    try:
        text_model = genai.GenerativeModel(GEMINI_MODEL_TEXT)
        print(f"[INFO] Carregado modelo Gemini: {GEMINI_MODEL_TEXT}")
    except Exception as e:
        print(f"[AVISO] Falha ao carregar modelo {GEMINI_MODEL_TEXT}: {e}")
        print("[AVISO] Tentando fallback para 'gemini-1.5-pro'...")
        text_model = genai.GenerativeModel("gemini-1.5-pro")
        GEMINI_MODEL_TEXT = "gemini-1.5-pro"
        print(f"[INFO] Fallback bem-sucedido, usando: {GEMINI_MODEL_TEXT}")
else:
    text_model = None


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

    Aqui lemos a variável diretamente do ambiente para debug:
    isso mostra exatamente o que o container do Render enxerga.
    """
    env_val = os.getenv("GEMINI_API_KEY")

    return {
        "service": "api-juridica",
        "gemini_env_present": env_val is not None,
        "gemini_env_prefix": (env_val[:6] + "...") if env_val else None,
        "gemini_configured": bool(env_val),
        "gemini_model": GEMINI_MODEL_TEXT if env_val else None,
    }


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
        "status": "done",            # simplificado: ingestão finalizada
        "progress": 100,
        "detail": "Ingestão concluída (simples)",
        "file_path": save_path,
        "case_number": case_number,
        "client_id": client_id,
        # meta poderá guardar, por exemplo, páginas de planilhas/SISBAJUD
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
# COM TENTATIVA DE EXTRAÇÃO DE PLANILHAS (TABELAS)
# E RETORNO DE METADADOS (PÁGINAS RELEVANTES)
# ============================================================

def _extract_text_from_pdf(path: str) -> tuple[str, Dict[str, Any]]:
    """
    Extrai texto do PDF combinando:
      1) PÁGINAS RELEVANTES ("hotspots") que contenham palavras-chave
         ligadas a planilhas, demonstrativos, SISBAJUD/BACENJUD, bloqueios etc.
         Nessas páginas, além do texto, tenta extrair tabelas e inclui o conteúdo.
      2) AMOSTRAGEM GLOBAL em 4 blocos (início, meio, pré-final e final).

    Retorna:
      - final_text: string com o texto que será enviado ao modelo.
      - meta: dict com metadados, ex. {"planilha_pages": [573, 587, ...]}
    """

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

    # Texto completo (para amostragem global)
    full_text = "\n\n".join(text_by_page) if text_by_page else ""
    total_len = len(full_text)

    if total_len == 0:
        return "", {"planilha_pages": []}

    # Valor vindo do .env
    env_max = int(os.getenv("MAX_PDF_CHARS", "30000"))

    # TETO DURO para não matar o modelo / gerar timeout absurdo
    HARD_CAP_CHARS = 80000  # pode ajustar (60000–80000) conforme performance

    # Limite efetivo a ser usado
    max_chars = min(env_max, HARD_CAP_CHARS)

    print(
        f"[INFO] MAX_PDF_CHARS(.env)={env_max} | HARD_CAP_CHARS={HARD_CAP_CHARS} "
        f"| usando max_chars={max_chars} | total_len={total_len}"
    )

    # Se todo o texto couber, não fazemos cortes
    if total_len <= max_chars:
        print(
            f"[INFO] Texto do PDF com {total_len} caracteres, "
            f"abaixo ou igual ao limite efetivo {max_chars}. Usando texto completo."
        )
        # Mesmo assim, vamos tentar marcar páginas relevantes
        planilha_pages = _detect_planilha_pages(text_by_page)
        return full_text, {"planilha_pages": planilha_pages}

    # --------------------------------------------------------
    # 1) HOTSPOTS: páginas com "planilha", "sisbajud", etc.
    #    Nessas páginas, além do texto, tentamos extrair tabelas.
    # --------------------------------------------------------
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

        # Verifica se esta página contém alguma palavra-chave
        if any(k in tl for k in keywords):
            page_num = idx + 1  # 1-based, como no processo
            planilha_pages.append(page_num)

            bloco = [f"\n\n=== PÁGINA RELEVANTE {page_num} (palavras-chave localizadas) ===\n\n"]
            bloco.append(raw_page_text)

            # Tenta extrair tabelas dessa página
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

    # Se não houver hotspots, só usamos a amostragem global
    if not hotspot_text:
        print("[INFO] Nenhuma página com palavras-chave relevantes encontrada. Usando apenas amostragem global.")
        global_sample = _build_global_sample(full_text, max_chars)
        return global_sample, {"planilha_pages": []}

    # Se houver hotspots, limitamos para não estourar o espaço inteiro
    max_hotspot = int(max_chars * 0.6)  # no máx. 60% do orçamento para hotspots
    if len(hotspot_text) > max_hotspot:
        print(
            f"[AVISO] Texto de hotspots com {len(hotspot_text)} caracteres, "
            f"maior que max_hotspot={max_hotspot}. Truncando hotspots."
        )
        hotspot_text = hotspot_text[:max_hotspot]

    remaining_chars = max_chars - len(hotspot_text)
    if remaining_chars <= 0:
        print(
            f"[AVISO] Espaço de caracteres esgotado somente com hotspots "
            f"(len={len(hotspot_text)} >= max_chars={max_chars}). "
            "Retornando apenas hotspots."
        )
        return hotspot_text, {"planilha_pages": planilha_pages}

    # --------------------------------------------------------
    # 2) AMOSTRAGEM GLOBAL (início + meio + pré-final + final)
    #    usando apenas o espaço restante.
    # --------------------------------------------------------
    global_sample = _build_global_sample(full_text, remaining_chars)

    final_text = (
        hotspot_text
        + "\n\n=== AMOSTRAGEM GLOBAL DO PROCESSO ===\n\n"
        + global_sample
    )

    print(
        f"[INFO] Texto final montado com len={len(final_text)} (máx={max_chars}), "
        f"hotspots_len={len(hotspot_text)}, global_len={len(global_sample)}"
    )

    return final_text, {"planilha_pages": planilha_pages}


def _detect_planilha_pages(text_by_page: list[str]) -> list[int]:
    """
    Detecta páginas que tenham palavras-chave de planilha/SISBAJUD
    quando estamos no cenário em que o texto inteiro coube no limite.
    """
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
    """
    Monta uma amostragem global em 4 blocos (início, meio, pré-final, final)
    respeitando max_chars.
    """
    total_len = len(full_text)
    if total_len <= max_chars:
        return full_text

    # Divide o limite em 4 partes
    part = max_chars // 4
    if part == 0:
        part = max_chars

    # 1) INÍCIO
    inicio = full_text[:part]

    # 2) MEIO (centralizado)
    mid_center = total_len // 2
    mid_start = max(0, mid_center - part // 2)
    mid_end = min(total_len, mid_start + part)
    meio = full_text[mid_start:mid_end]

    # 3) PRÉ-FINAL (região antes do final, ~2 partes a partir do fim)
    pre_final_start = max(0, total_len - (part * 2))
    pre_final_end = pre_final_start + part
    if pre_final_end > total_len:
        pre_final_end = total_len
        pre_final_start = max(0, pre_final_end - part)
    pre_final = full_text[pre_final_start:pre_final_end]

    # 4) FINAL
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


# ============================================================
# "AGENTES" DE EXECUÇÃO: VÁRIAS PERGUNTAS → UM RELATÓRIO
# (PROMPTS MAIS DETALHADOS/EXAUSTIVOS)
# ============================================================

def _run_execucao_agents(base_text: str, case_number: str, action_type: str) -> tuple[str, dict]:
    """
    Envia VÁRIAS PERGUNTAS separadas para o Gemini (uma por seção)
    e monta um relatório final em Markdown.

    Retorna:
      - final_md: relatório completo
      - sections: dict com o texto bruto de cada seção
    """

    if not text_model:
        raise RuntimeError("Modelo Gemini não está configurado (text_model=None).")

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

REGRAS ESPECIAIS PARA ADVOGADOS:
- Sempre identifique quem cada advogado representa:
  • Se o nome aparece em petição ou assinatura do BANCO / EXEQUENTE, considere como "Advogado do Exequente".
  • Se o nome aparece em petição ou assinatura de EXECUTADO (devedor/avalista), considere como "Advogado dos Executados".
- NUNCA repita o mesmo advogado nas duas listas. Em caso de dúvida, prefira classificá-lo como "Advogado dos Executados".
- Se houver menção a publicação/intimação exclusiva em nome de determinado advogado, mantenha-o na lista correta (Exequente ou Executados).

Outras regras:
- Varra todo o texto com atenção; liste TUDO que encontrar, mesmo que pareça repetido.
- Se algum item não aparecer, escreva "Não informado".
- Sempre que houver número de documento ou folha/página (ex.: fls. 573), mencione entre parênteses.
- Responda em Markdown começando com o título "## Cabeçalho" e bullets iniciando com "• ".
"""
        },
        {
            "key": "resumo_inicial",
            "title": "Resumo da Petição Inicial",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO DE TÍTULO EXTRAJUDICIAL.

Sua tarefa aqui é fazer um **resumo rico em detalhes**, não superficial.

Com base EXCLUSIVAMENTE no texto abaixo, elabore o **RESUMO DA PETIÇÃO INICIAL**, contendo:

1. Partes:
   - Quem está processando quem? (exequente x executados, com nomes completos).
   - Se houver fiadores/avalistas, mencionar expressamente.

2. Origem da dívida:
   - Tipo de contrato (ex.: Cédula de Crédito Bancário, confissão de dívida, cheque etc.).
   - Número do documento, data de emissão, valor original e principais condições, se constarem.
   - Se houve renegociação ou confissão posterior, descreva.

3. Valores:
   - Informe o valor executado na inicial (valor e data da atualização).
   - Se a petição mencionar juros, multa, comissão de permanência, encargos, descreva brevemente.

4. Garantias:
   - Quais garantias são invocadas? (penhor, hipoteca, aval, fiança, alienação fiduciária, etc.)
   - Descrever sucintamente o bem ou obrigação garantida, se constar.

5. Pedidos:
   - Liste, em bullets, os pedidos formulados na inicial (ex.: citação, penhora, BACENJUD, RENAJUD, custas, honorários).

Regras:
- Seja detalhado: prefira pecar pelo excesso de informação do que pela falta.
- Use entre 3 e 8 parágrafos, além de bullets para os pedidos.
- Não invente fatos; se algo não constar, simplesmente não mencione ou indique "Não informado".
- Comece com o título "## Resumo da Petição Inicial" em Markdown.
"""
        },
        {
            "key": "penhora",
            "title": "Tentativas de Penhora Online e Garantias",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Aqui você deve focar especificamente nas **buscas de bens e garantias**.

Com base EXCLUSIVAMENTE no texto abaixo, faça uma seção chamada
"## Tentativas de Penhora Online e Garantias" contendo:

1. Sistemas de busca de bens:
   Para cada sistema listado abaixo, indique com o máximo de detalhes:
   - se há pedido,
   - se há decisão deferindo/indeferindo,
   - se houve efetiva realização (cumprimento) e resultado,
   - datas, valores e menção a folhas/páginas, se houver.

   Sistemas:
   - RENAJUD
   - SISBAJUD/BACENJUD
   - INFOJUD
   - SERASAJUD
   - outros (ex.: pesquisas em cartórios, CCS, SIEL, etc.)

2. Garantias e constrições:
   - penhora de bens móveis (descrição do bem, valor da avaliação se constar, fls.)
   - penhora de imóveis (nº da matrícula, cartório, descrição básica, fls.)
   - arrestos, sequestros, indisponibilidades
   - registro de penhora em matrícula de imóvel
   - qualquer outra medida cautelar sobre bens.

3. Se NÃO encontrar qualquer menção a determinado sistema,
   NÃO afirme que não houve no processo inteiro.
   Em vez disso, escreva algo como:
   "Não há informação nos trechos analisados sobre utilização de [NOME DO SISTEMA]."

Regras:
- Liste cada medida em bullets, com datas e valores sempre que aparecerem.
- Não invente informação nem conclua negativamente sobre o processo todo; limite-se aos trechos analisados.
"""
        },
        {
            "key": "valores_planilhas",
            "title": "Valores e Planilhas de Débito",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Sua tarefa aqui é **minerar todos os valores e planilhas** que apareçam no texto.

Crie uma seção "## Valores e Planilhas de Débito" contendo:

1. Valor original da operação:
   - Liste TODO valor que pareça ser o valor original do contrato/operação, com data e documento associado.

2. Valor executado na inicial:
   - Liste o(s) valor(es) indicado(s) como débito executado, com datas de atualização
     e referência a planilhas (ex.: "conforme planilha de fls. 573").

3. Planilhas de cálculo / demonstrativos:
   - Para CADA planilha ou demonstrativo localizado no texto, liste:
     • Data de referência da atualização (se constar)  
     • Valor total atualizado  
     • Referência de folha/página (fls.) ou descrição do documento  
   - Se localizar planilhas em anos diferentes (ex.: 2016, 2019, 2023), deixe claro em ordem cronológica.

4. Evolução dos valores:
   - Se localizar mais de uma planilha, faça um quadro em Markdown
     mostrando a evolução temporal, neste formato:

     | Data de Referência | Valor Atualizado | Observação |
     | :----------------- | :-------------- | :--------- |
     | dd/mm/aaaa         | R$ X            | Ex.: "planilha inicial" |
     | dd/mm/aaaa         | R$ Y            | Ex.: "planilha posterior" |

   - Se encontrar apenas **uma** planilha, ainda assim crie a tabela com uma única linha.

5. Bloqueios via SISBAJUD/BACENJUD:
   - Se houver bloqueios efetivos (não só pedido), liste:
     • data do bloqueio  
     • valor bloqueado  
     • conta/titular, se constar  
     • desfecho (ex.: convertido em penhora, desbloqueado etc.), se houver.

Regras importantes:
- Varra o texto com atenção a qualquer "R$" e datas, correlacionando com contexto (inicial, planilha, bloqueio).
- NÃO invente valores ou datas.
- Se não encontrar alguma parte (ex.: planilhas posteriores), escreva explicitamente:
  "Não foram localizadas planilhas posteriores nos trechos analisados."
"""
        },
        {
            "key": "movimentacoes",
            "title": "Movimentações Processuais Relevantes",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Agora você vai montar uma **linha do tempo detalhada**.

Crie uma seção "## Movimentações Processuais Relevantes" contendo uma linha por ato relevante,
NO MÍNIMO para:

- distribuição da ação
- citação
- apresentação de embargos, exceções, incidentes (falsidade, desconsideração etc.)
- decisões de mérito ou relevantes (deferimentos, indeferimentos, extinções, suspensões)
- juntada de planilhas de débito, laudos, perícias
- decisões sobre prescrição/prescrição intercorrente
- decisões sobre SISBAJUD/RENAJUD/INFOJUD etc.
- sentenças e decisões de instâncias superiores
- principais atos entre 2018 e 2025 (se houver).

Formato:

• dd/mm/aaaa (ou ano aproximado, se for o caso): descrição objetiva do ato, mencionando fls. quando constar.

Regras:
- PREFIRA listar muitos atos a poucos. Se houver muita movimentação, foque nas relevantes para:
  valor do crédito, garantias, prosseguimento/suspensão/extinção da execução.
- Se a data exata não constar, use algo como "2008 (data não informada): ...".
- Não faça comentários jurídicos aqui; apenas descreva os atos.
"""
        },
        {
            "key": "analise_juridica",
            "title": "Análise Jurídica",
            "instruction": f"""
Você é um assistente jurídico especialista em EXECUÇÃO.

Agora você deve organizar uma **visão consolidada**, em bullets, baseada APENAS no que consta no texto.

Crie a seção "## Análise Jurídica" com os seguintes itens (mantendo a ordem exata abaixo),
sempre preenchendo cada linha com alguma informação ou, se nada constar, escrevendo "Não informado":

• Exequente: (dados completos disponíveis)  
• Advogados do Exequente: (nomes e OAB, se constarem)  
• Executados: (dados completos disponíveis)  
• Advogados dos Executados: (nomes e OAB)  
• Fiadores, Avalistas ou Terceiros Garantidores: (se houver, descrever função)  
• Assinatura de Documentos: (quem assinou cada documento relevante: contrato, confissão de dívida, procurações)  
• Citação e Intimação: (como ocorreu, quem foi citado, quando, se houve problemas)  
• Validade das Citações: (se há notícia de nulidade, ou se nada constar → "Não informado")  
• Bens Móveis em Garantia: (descrição dos bens; se não constar → "Não informado")  
• Penhora/Arresto: (se houve, sobre quais bens, valores envolvidos, datas principais)  
• Registro da Penhora: (se houve registro em matrícula; se nada constar → "Não informado")  
• Outras Penhoras: (em outros processos, se o texto mencionar)  
• Planilha de Cálculo: (resumir, em 1–3 linhas, a última planilha encontrada: data e valor)  
• Impugnação aos Cálculos: (se houve; se não constar, dizer "Não informado")  
• Homologação do Valor: (se houve decisão homologando o valor, mencionar data; senão, "Não informado")  
• Avaliação de Bens: (se houve pedido/realização, mencionar bens, datas e valores; senão, "Não informado")  
• Leilão: (se houve designação, realização ou cancelamento; senão, "Não informado")  
• Manifestação de Terceiros: (se houve embargos de terceiro ou outras intervenções)  
• Exceção de Pré-Executividade: (se houve; se não constar, "Não informado")  
• Embargos à Execução: (se houve, situação atual; se não constar, "Não informado")  
• Incidentes Processuais Relevantes: (ex.: incidente de falsidade, fraude, nulidade, desconsideração; descrever em 2–5 linhas)  
• Bloqueios e buscas de bens: (resumo do que foi efetivamente feito em SISBAJUD, RENAJUD, INFOJUD etc.; se nada constar, "Não informado")  
• Prescrição / Prescrição intercorrente: (se há decisão reconhecendo, indeferindo ou risco apontado; ou "Não informado")  
• Paralisação do Processo: (períodos longos sem movimentação relevantes, com anos aproximados; ou "Não informado").

Regras:
- Seja o mais factual e detalhado possível, sem emitir opiniões jurídicas, recomendações ou juízos de valor.
- Se não encontrar nada sobre um item, escreva exatamente "Não informado".
- Não resuma demais: se houver muita informação relevante, distribua em frases curtas dentro do mesmo bullet.
"""
        },
    ]

    sections: dict[str, str] = {}
    for task in tasks:
        prompt = f"""
{task["instruction"]}

=== TEXTO DO PROCESSO (EXTRAÍDO DO PDF) ===

\"\"\"{base_text}\"\"\""""
        print(f"[AGENTE] Rodando sub-tarefa: {task['key']} ({task['title']})")
        resp = text_model.generate_content(prompt)
        text = (resp.text or "").strip()
        sections[task["key"]] = text

    # Monta o relatório final
    md_parts: list[str] = []
    md_parts.append(f"Sumarização da {action_type} ({case_number})\n")

    order = [
        "cabecalho",
        "resumo_inicial",
        "penhora",
        "valores_planilhas",
        "movimentacoes",
        "analise_juridica",
    ]

    for key in order:
        section_text = sections.get(key, "").strip()
        if not section_text:
            title = next(t["title"] for t in tasks if t["key"] == key)
            md_parts.append(f"## {title}\n\nNão informado.")
        else:
            md_parts.append(section_text)

    final_md = "\n\n".join(md_parts)
    return final_md, sections


# ============================================================
# ENDPOINT /summarize - MANDANDO UMA PERGUNTA POR VEZ (AGENTES)
# ============================================================

@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    """
    Versão multi-agentes para EXECUÇÃO:

    1) Localiza o PDF correspondente ao número do processo.
    2) Extrai o texto (limitado) com hotspots (planilhas/SISBAJUD) + amostragem,
       e guarda as páginas de planilha/SISBAJUD em JOBS[job]["meta"].
    3) Envia VÁRIAS PERGUNTAS separadas para o Gemini (cabeçalho, resumo,
       penhora, valores/planilhas, movimentações, análise jurídica).
    4) Junta tudo em um relatório final em Markdown e devolve para o Streamlit.
    """
    try:
        case_number = req.case_number
        action_type = req.action_type

        # 1) Encontrar o job correspondente ao número do processo
        job = None
        for j in JOBS.values():
            if j.get("case_number") == case_number:
                job = j
                break

        if not job:
            raise HTTPException(
                status_code=404,
                detail="Nenhum job encontrado para esse número de processo"
            )

        file_path = job["file_path"]

        if not GEMINI_API_KEY or not text_model:
            raise HTTPException(
                status_code=500,
                detail="Gemini não configurado na API (.env)"
            )

        # 2) Extrair texto do PDF (hotspots + amostragem global, com limite)
        base_text, meta = _extract_text_from_pdf(file_path)
        if not base_text:
            base_text = "Não foi possível extrair texto do PDF. Verifique o arquivo."

        # Guarda metadados (ex.: páginas com planilhas/SISBAJUD) no job
        if meta:
            job_meta = job.get("meta") or {}
            job_meta.update(meta)
            job["meta"] = job_meta

        # 3) Rodar "multiagentes" de execução (várias perguntas → um relatório)
        final_md, sections = _run_execucao_agents(
            base_text=base_text,
            case_number=case_number,
            action_type=action_type,
        )

        return {
            "summary_markdown": final_md,
            "sections": sections,  # cada resposta separada (opcional usar no futuro)
            "used_chunks": [],     # futuro: integrar com RAG
            "result": {
                "meta": meta
            },          # futuro: JSON estruturado mais rico
        }

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print("ERRO EM /summarize:\n", tb)
        raise HTTPException(
            status_code=500,
            detail=f"{e.__class__.__name__}: {e}"
        )


# ============================================================
# ENDPOINT /export/docx - GERA DOCX A PARTIR DO MARKDOWN
# COM OPÇÃO DE INSERIR PRINTS DAS PÁGINAS DE PLANILHA/SISBAJUD
# ============================================================

@app.post("/export/docx")
async def export_docx(
    content: str = Form(...),
    filename: str = Form("relatorio.docx"),
    case_number: str | None = Form(None),
    include_planilha_images: bool = Form(False),
):
    """
    Recebe um texto em Markdown simples e devolve um DOCX.
    - Converte # e ## em títulos.
    - Se include_planilha_images=True e case_number fornecido, tenta:
        * localizar o job correspondente
        * ler meta["planilha_pages"]
        * gerar imagens dessas páginas (se pdf2image estiver disponível)
        * inserir no final do DOCX em "Anexos – Planilhas e Bloqueios Relevantes"
          com legenda "Planilha / demonstrativo – pág. X".
    """
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # Conteúdo principal (texto)
    for line in content.splitlines():
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        else:
            doc.add_paragraph(line)

    # Se for para incluir prints de planilhas/SISBAJUD
    if include_planilha_images and case_number:
        if not PDF2IMAGE_AVAILABLE:
            print("[AVISO] include_planilha_images=True, mas pdf2image não está disponível. Nenhuma imagem será inserida.")
        else:
            # Encontrar job pelo número do processo
            job = None
            for j in JOBS.values():
                if j.get("case_number") == case_number:
                    job = j
                    break

            if job:
                meta = job.get("meta") or {}
                planilha_pages = meta.get("planilha_pages") or []
                planilha_pages = sorted(set(planilha_pages))

                if planilha_pages:
                    file_path = job.get("file_path")
                    if file_path and os.path.exists(file_path):
                        try:
                            # Converte o PDF inteiro em imagens (lista de páginas)
                            print(f"[INFO] Gerando imagens das páginas {planilha_pages} do PDF para anexar no DOCX...")
                            images = convert_from_path(file_path)

                            # Insere seção de anexos no final
                            doc.add_page_break()
                            doc.add_heading("Anexos – Planilhas e Bloqueios Relevantes", level=1)

                            for p in planilha_pages:
                                if 1 <= p <= len(images):
                                    img = images[p - 1]
                                    img_bytes = io.BytesIO()
                                    img.save(img_bytes, format="PNG")
                                    img_bytes.seek(0)

                                    # Legenda / título antes da imagem
                                    doc.add_paragraph(f"Planilha / demonstrativo – pág. {p}")
                                    # Insere imagem – largura fixa para caber na página
                                    doc.add_picture(img_bytes, width=Inches(6.0))
                                    doc.add_paragraph("")  # espaço depois da imagem
                        except Exception as e:
                            print(f"[AVISO] Falha ao gerar/ inserir imagens de planilha no DOCX: {e}")
                    else:
                        print("[AVISO] Caminho do PDF não encontrado ao tentar gerar imagens de planilha.")
                else:
                    print("[INFO] Nenhuma página marcada como planilha/SISBAJUD em meta['planilha_pages'].")
            else:
                print("[AVISO] Nenhum job encontrado em memória para o case_number informado ao exportar DOCX.")

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
