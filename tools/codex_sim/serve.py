"""python -X utf8 tools/codex_sim/serve.py [--port 8663] [--host 127.0.0.1]"""
import argparse
import sys
from runtime import configure, ROOT
ap = argparse.ArgumentParser()
ap.add_argument('--port', type=int, default=8663)
ap.add_argument('--host', default='127.0.0.1')
args = ap.parse_args()
configure()
sys.path.insert(0, str(ROOT))
import time_machine
import uvicorn
with time_machine.travel('2026-10-11T09:00:00+01:00', tick=False):
    uvicorn.run('app.main:app', host=args.host, port=args.port, access_log=False, log_level='warning')
