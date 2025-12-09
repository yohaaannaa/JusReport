#!/usr/bin/env bash
# ativar ambiente virtual (ajuste se estiver no Windows)
source .venv/bin/activate
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000
