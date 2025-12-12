import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

# ============================================================
# DIRETÓRIO DE DADOS (importante na nuvem)
# - Local: usa a pasta do projeto (JusReport/data)
# - Streamlit Cloud: é melhor usar um diretório gravável e estável do usuário
#   (HOME/.jusreport_data) para evitar problemas com permissões e resets.
# ============================================================

def _resolve_data_root() -> Path:
    """
    Ordem de prioridade:
    1) JUSREPORT_DATA_DIR (se você quiser controlar via Secrets)
    2) Pasta do projeto (BASE_DIR/data) se gravável
    3) HOME/.jusreport_data (fallback seguro na nuvem)
    """
    env_dir = os.getenv("JUSREPORT_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    # Pasta do projeto
    base_dir = Path(__file__).resolve().parents[2]  # JusReport/app/utils/db.py -> JusReport
    project_data = (base_dir / "data").resolve()

    # tenta usar data/ do projeto
    try:
        project_data.mkdir(exist_ok=True, parents=True)
        test_file = project_data / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return project_data
    except Exception:
        # fallback seguro
        home_data = (Path.home() / ".jusreport_data").resolve()
        home_data.mkdir(exist_ok=True, parents=True)
        return home_data


DATA_DIR = _resolve_data_root()
UPLOAD_DIR = DATA_DIR / "uploads"
REL_DIR = DATA_DIR / "relatorios"
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
REL_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "banco_dados.db"


# ============================================================
# CONEXÃO E INIT
# ============================================================

def _get_conn() -> sqlite3.Connection:
    # timeout evita "database is locked"
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
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
    """)

    conn.commit()
    conn.close()


def _ensure_schema() -> None:
    """
    Migração simples: garante que a tabela existe e que colunas críticas existem.
    (Se você já tiver DB antigo, isso evita quebrar.)
    """
    _init_db()
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(processos)")
    cols = {row["name"] for row in cur.fetchall()}

    needed = {
        "id": "TEXT",
        "nome_cliente": "TEXT",
        "email": "TEXT",
        "numero_processo": "TEXT",
        "tipo": "TEXT",
        "conferencia": "TEXT",
        "data_envio": "TEXT",
        "caminho_arquivo": "TEXT",
        "status": "TEXT",
        "caminho_relatorio": "TEXT",
    }

    for col, ctype in needed.items():
        if col not in cols:
            cur.execute(f"ALTER TABLE processos ADD COLUMN {col} {ctype}")

    conn.commit()
    conn.close()


_ensure_schema()


# ============================================================
# FUNÇÕES
# ============================================================

def salvar_processo(
    nome_cliente: str,
    email: str,
    numero: str,
    tipo: str,
    arquivo,
    conferencia: str
) -> str:
    """
    Salva o arquivo em DATA_DIR/uploads e registra no banco como 'pendente'.
    """
    from uuid import uuid4
    proc_id = str(uuid4())

    ext = os.path.splitext(getattr(arquivo, "name", "arquivo"))[1] or ".bin"
    file_name = f"{proc_id}{ext}"
    file_path = UPLOAD_DIR / file_name

    # streamlit uploader tem getvalue()
    with open(file_path, "wb") as f:
        f.write(arquivo.getvalue())

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO processos
        (id, nome_cliente, email, numero_processo, tipo, conferencia, data_envio, caminho_arquivo, status, caminho_relatorio)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        proc_id,
        nome_cliente,
        email,
        numero,
        tipo,
        conferencia,
        datetime.now().isoformat(),
        str(file_path),
        "pendente",
        None,
    ))
    conn.commit()
    conn.close()

    return proc_id


def listar_processos(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lista processos. Se status=None, lista todos.
    """
    try:
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

    except sqlite3.OperationalError:
        # Se der erro por tabela/arquivo, tenta re-criar e reexecutar
        _ensure_schema()
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
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE processos SET status = ? WHERE id = ?", (novo_status, proc_id))
    conn.commit()
    conn.close()


def registrar_relatorio(proc_id: str, caminho_docx: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE processos SET caminho_relatorio = ?, status = ? WHERE id = ?",
        (caminho_docx, "finalizado", proc_id),
    )
    conn.commit()
    conn.close()
