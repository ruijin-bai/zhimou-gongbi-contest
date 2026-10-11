"""Isolated simulation runtime; environment is established before any app import."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'data_codex'

def configure():
    DATA.mkdir(exist_ok=True)
    envfile=DATA/'isolated.env'
    if not envfile.exists(): envfile.write_text('',encoding='utf-8')
    # Do not inspect inherited secrets or load the repository .env.
    for key in ('ZB_AI_API_KEY','ZB_AI_API_KEYS','ZHIMOU_AI_API_KEY','ZB_SMTP_PASSWORD','ZB_WEBHOOK_URL'):
        os.environ.pop(key, None)
    os.environ.update(ZB_DATA_DIR=str(DATA), ZB_ENV_FILE=str(DATA/'isolated.env'),
                      ZB_AI_PROVIDER='disabled', ZB_SCHEDULER='0', ZB_DEV_LOGIN='1',
                      ZB_DEV_PROJECT='SIM-ROAD', ZB_DEMO_DAY='2026-10-11',
                      ZB_SECRET='codex-simulation-session-only', ZB_HOST='127.0.0.1')
