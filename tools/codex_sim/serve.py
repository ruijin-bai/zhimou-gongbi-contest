"""python -X utf8 tools/codex_sim/serve.py --port 8663"""
import sys
from runtime import configure, ROOT
configure()
sys.path.insert(0, str(ROOT))
import time_machine
import uvicorn
with time_machine.travel('2026-10-11T09:00:00+01:00', tick=False):
    uvicorn.run('app.main:app', host='127.0.0.1', port=8663,access_log=False,log_level='warning')
