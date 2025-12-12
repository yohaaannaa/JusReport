import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path


# ============================================================
# DATA DIR: local (repo/data) vs cloud (/tmp)
# ============================================================

def _data_dir() -> Path:
    # Se quiser forçar via env:
    forced = os.getenv("JUSREPORT_DATA_DIR")
    if forced:
        p = Path(forced).expanduser().resolve()
        p.mkdir(exist_ok=True, parents=True)
        return p

    # Streamlit Cloud roda em Linux -> /tmp é gravável
    if os.name != "nt":
        p = Path("/tmp/jusreport_data")
        p.mkdir(exist_ok=True, parents=True)
        return p

    # Windows local: raiz do projeto
    base_dir = Path(__file__).resolve().parents[2]  # JusReport/app/utils/db.py -> JusReport
    p = base_dir / "data"
    p.mkdir(exist_ok=True, parents=True)
    return p


DATA_DIR = _data_dir()
UPLOAD_DIR = DATA_DIR / "uploads"
REL_DIR = DATA_DIR / "relatorios"
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
REL_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "banco_dados.db"


# ============================================================
# SQLITE: conexão + init (com retry)
# ============================================================

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()
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


# inicializa sempre
_init_db()


# ============================================================
# FUNÇÕES PÚBLICAS
# ============================================================

def salvar_processo(nome_cliente: str, email: str, numero: str, tipo: str, arquivo, conferencia: str) -> str:
    """
    Salva o processo no disco e registra no banco como 'pendente'.
    """
    from uuid import uuid4

    _init_db()

    proc_id = str(uuid4())

    ext = os.path.splitext(getattr(arquivo, "name", "") or "")[1] or ".bin"
    file_name = f"{proc_id}{ext}"
    file_path = UPLOAD_DIR / file_name

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
    """
    ULTRA-DEFENSIVO: se o banco estiver corrompido, inacessível ou a tabela não existir,
    reinicializa e devolve lista vazia (sem derrubar o app).
    """
    try:
        _init_db()
        conn = _get_conn()
        cur = conn.cursor()

        if status:
            cur.execute("SELECT * FROM processos WHERE status = ? ORDER BY data_envio DESC", (status,))
        else:
            cur.execute("SELECT * FROM processos ORDER BY data_envio DESC")

        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    except sqlite3.OperationalError:
        # tenta reinicializar e não derruba o app
        try:
            _init_db()
        except Exception:
            pass
        return []


def atualizar_status(proc_id: str, novo_status: str) -> None:
    _init_db()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE processos SET status = ? WHERE id = ?", (novo_status, proc_id))
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
