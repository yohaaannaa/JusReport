import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path


# ============================================================
# PATHS COMPATÍVEIS COM LOCAL + STREAMLIT CLOUD
# ============================================================

def _resolve_data_dir() -> Path:
    """
    Onde o SQLite e os arquivos (uploads/relatorios) serão gravados.

    - LOCAL (Windows): usa <projeto>/data
    - STREAMLIT CLOUD (Linux): usa /tmp/jusreport_data (sempre gravável)

    Se você quiser forçar manualmente um caminho (ex.: um volume),
    defina: JUSREPORT_DATA_DIR=/algum/caminho
    """
    env_dir = os.getenv("JUSREPORT_DATA_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        p.mkdir(exist_ok=True, parents=True)
        return p

    # Linux (Streamlit Cloud / Render etc.)
    if os.name != "nt":
        p = Path("/tmp/jusreport_data")
        p.mkdir(exist_ok=True, parents=True)
        return p

    # Windows local: raiz do projeto = subir 3 níveis (app/utils/db.py -> JusReport)
    base_dir = Path(__file__).resolve().parents[2]
    p = base_dir / "data"
    p.mkdir(exist_ok=True, parents=True)
    return p


DATA_DIR = _resolve_data_dir()

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)

REL_DIR = DATA_DIR / "relatorios"
REL_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "banco_dados.db"


# ============================================================
# SQLITE: conexão + init
# ============================================================

def _get_conn() -> sqlite3.Connection:
    # timeout evita falhas em lock; check_same_thread=False ajuda no Streamlit
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()

    # Mantive os nomes das colunas do seu schema para não quebrar seu UI:
    # - status
    # - caminho_relatorio
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS processos (
            id TEXT PRIMARY KEY,
            nome_cliente TEXT,
            email TEXT,
            numero_processo TEXT,
            tipo TEXT,
            conferencia TEXT,
            data_envio TEXT,
            caminho_arquivo TEXT,
            status TEXT,
            caminho_relatorio TEXT
        )
        """
    )

    conn.commit()
    conn.close()


_init_db()


# ============================================================
# CRUD
# ============================================================

def salvar_processo(
    nome_cliente: str,
    email: str,
    numero: str,
    tipo: str,
    arquivo,  # Streamlit UploadedFile
    conferencia: str
) -> str:
    """
    Salva o arquivo no disco e registra no banco como 'pendente'.
    """
    from uuid import uuid4

    _init_db()

    proc_id = str(uuid4())

    # Nome do arquivo: tenta manter extensão original
    ext = os.path.splitext(getattr(arquivo, "name", "") or "")[1] or ".bin"
    file_name = f"{proc_id}{ext}"
    file_path = UPLOAD_DIR / file_name

    # Streamlit UploadedFile:
    # - pode ter .getbuffer() (recomendado)
    # - ou .getvalue()
    try:
        content = arquivo.getbuffer()
    except Exception:
        content = arquivo.getvalue()

    with open(file_path, "wb") as f:
        f.write(content)

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO processos
        (id, nome_cliente, email, numero_processo, tipo, conferencia, data_envio, caminho_arquivo, status, caminho_relatorio)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proc_id,
            nome_cliente,
            email,
            numero,
            tipo,
            conferencia,
            datetime.now().isoformat(timespec="seconds"),
            str(file_path),
            "pendente",
            None,
        ),
    )
    conn.commit()
    conn.close()
    return proc_id


def listar_processos(status: Optional[str] = None) -> List[Dict[str, Any]]:
    _init_db()

    conn = _get_conn()
    cur = conn.cursor()

    if status:
        cur.execute(
            "SELECT * FROM processos WHERE status = ? ORDER BY data_envio DESC",
            (status,),
        )
    else:
        cur.execute("SELECT * FROM processos ORDER BY data_envio DESC")

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def atualizar_status(proc_id: str, novo_status: str) -> None:
    _init_db()

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE processos SET status = ? WHERE id = ?",
        (novo_status, proc_id),
    )
    conn.commit()
    conn.close()


def registrar_relatorio(proc_id: str, caminho_docx: str) -> None:
    _init_db()

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE processos SET caminho_relatorio = ?, status = ? WHERE id = ?",
        (caminho_docx, "finalizado", proc_id),
    )
    conn.commit()
    conn.close()
