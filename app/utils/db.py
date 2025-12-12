import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path
from uuid import uuid4


def _resolve_data_dir() -> Path:
    """
    Garante pasta gravável no Streamlit Cloud.
    1) tenta usar ./data (no projeto)
    2) se não conseguir escrever, usa ~/.jusreport_data
    """
    base_dir = Path(__file__).resolve().parents[2]  # raiz do projeto
    local_data = base_dir / "data"

    try:
        local_data.mkdir(exist_ok=True, parents=True)
        test_file = local_data / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return local_data
    except Exception:
        cloud_data = Path.home() / ".jusreport_data"
        cloud_data.mkdir(exist_ok=True, parents=True)
        return cloud_data


DATA_DIR = _resolve_data_dir()
UPLOAD_DIR = DATA_DIR / "uploads"
REL_DIR = DATA_DIR / "relatorios"
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
REL_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "banco_dados.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    conn = _get_conn()
    cur = conn.cursor()

    # 1) cria tabela SEMPRE
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


# roda ao importar (isso garante que a tabela exista ANTES de qualquer SELECT)
_ensure_schema()


def salvar_processo(nome_cliente: str, email: str, numero: str, tipo: str, arquivo, conferencia: str) -> str:
    _ensure_schema()
    proc_id = str(uuid4())

    ext = os.path.splitext(getattr(arquivo, "name", "") or "")[1] or ".pdf"
    file_name = f"{proc_id}{ext}"
    file_path = UPLOAD_DIR / file_name

    with open(file_path, "wb") as f:
        f.write(arquivo.getvalue())

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
            datetime.now().isoformat(),
            str(file_path),
            "pendente",
            None,
        ),
    )
    conn.commit()
    conn.close()
    return proc_id


def listar_processos(status: Optional[str] = None) -> List[Dict[str, Any]]:
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
    _ensure_schema()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE processos SET status = ? WHERE id = ?", (novo_status, proc_id))
    conn.commit()
    conn.close()


def registrar_relatorio(proc_id: str, caminho_docx: str) -> None:
    _ensure_schema()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE processos SET caminho_relatorio = ?, status = ? WHERE id = ?",
        (caminho_docx, "finalizado", proc_id),
    )
    conn.commit()
    conn.close()
