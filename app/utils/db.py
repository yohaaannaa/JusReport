import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "banco_dados.db"
REL_DIR = DATA_DIR / "relatorios"
REL_DIR.mkdir(exist_ok=True, parents=True)

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
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

_init_db()

def salvar_processo(nome_cliente: str, email: str, numero: str, tipo: str, arquivo, conferencia: str) -> str:
    """
    Salva o processo no disco e registra no banco como 'pendente'.
    """
    from uuid import uuid4
    proc_id = str(uuid4())

    uploads_dir = DATA_DIR / "uploads"
    uploads_dir.mkdir(exist_ok=True, parents=True)

    ext = os.path.splitext(arquivo.name)[1]
    file_name = f"{proc_id}{ext}"
    file_path = uploads_dir / file_name

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

def listar_processos(status: str | None = None) -> List[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    if status:
        cur.execute("SELECT * FROM processos WHERE status = ? ORDER BY data_envio DESC", (status,))
    else:
        cur.execute("SELECT * FROM processos ORDER BY data_envio DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def atualizar_status(proc_id: str, novo_status: str):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE processos SET status = ? WHERE id = ?", (novo_status, proc_id))
    conn.commit()
    conn.close()

def registrar_relatorio(proc_id: str, caminho_docx: str):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE processos SET caminho_relatorio = ?, status = ? WHERE id = ?", (caminho_docx, "finalizado", proc_id))
    conn.commit()
    conn.close()
