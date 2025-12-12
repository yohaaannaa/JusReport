import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

def _resolve_data_root() -> Path:
    env_dir = os.getenv("JUSREPORT_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    base_dir = Path(__file__).resolve().parents[2]
    project_data = (base_dir / "data").resolve()

    # /mount/src/... pode ser read-only no Streamlit Cloud
    try:
        project_data.mkdir(exist_ok=True, parents=True)
        test_file = project_data / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return project_data
    except Exception:
        home_data = (Path.home() / ".jusreport_data").resolve()
        home_data.mkdir(exist_ok=True, parents=True)
        return home_data

DATA_DIR = _resolve_data_root()
UPLOAD_DIR = DATA_DIR / "uploads"
REL_DIR = DATA_DIR / "relatorios"
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
REL_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "banco_dados.db"

print(f"[DB] DATA_DIR={DATA_DIR}")
print(f"[DB] DB_PATH={DB_PATH}")

def _get_conn() -> sqlite3.Connection:
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

try:
    _ensure_schema()
except Exception as e:
    print(f"[DB][ERRO] Falha ao inicializar schema: {type(e).__name__}: {e}")
    raise

def salvar_processo(nome_cliente: str, email: str, numero: str, tipo: str, arquivo, conferencia: str) -> str:
    from uuid import uuid4
    proc_id = str(uuid4())

    ext = os.path.splitext(getattr(arquivo, "name", "arquivo"))[1] or ".bin"
    file_name = f"{proc_id}{ext}"
    file_path = UPLOAD_DIR / file_name

    with open(file_path, "wb") as f:
        f.write(arquivo.getvalue())

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO processos
        (id, nome_cliente, email, numero_processo, tipo, conferencia, data_envio, caminho_arquivo, status, caminho_relatorio)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        proc_id, nome_cliente, email, numero, tipo, conferencia,
        datetime.now().isoformat(), str(file_path), "pendente", None
    ))
    conn.commit()
    conn.close()
    return proc_id

def listar_processos(status: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if status:
            cur.execute("SELECT * FROM processos WHERE status = ? ORDER BY data_envio DESC", (status,))
        else:
            cur.execute("SELECT * FROM processos ORDER BY data_envio DESC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    except Exception as e:
        # Isso vai aparecer no LOG do Streamlit (Manage app -> Logs)
        print(f"[DB][ERRO] listar_processos falhou: {type(e).__name__}: {e}")
        # Tenta recriar schema e repetir 1 vez
        _ensure_schema()
        conn = _get_conn()
        cur = conn.cursor()
        if status:
            cur.execute("SELECT * FROM processos WHERE status = ? ORDER BY data_envio DESC", (status,))
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
