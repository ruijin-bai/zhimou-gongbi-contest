"""Read repository source and initialize only our isolated schema; never open another DB."""
import os, sys, pathlib, ast, re, json
from runtime import configure,ROOT,DATA
configure()
sys.path.insert(0, str(ROOT))
from app.db import init_db, connect
init_db()
c = connect()
inventory = []
for p in sorted((ROOT / 'app').rglob('*.py')):
    src = p.read_text(encoding='utf-8-sig')
    tree = ast.parse(src)
    inventory.append({'file':str(p.relative_to(ROOT)), 'functions':[n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))], 'tables':re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)',src), 'reads':sorted(set(re.findall(r'(?:FROM|JOIN)\s+(\w+)',src,re.I)))})
(DATA / 'code_inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2),encoding='utf-8')
tables = c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
(DATA / 'schema.json').write_text(json.dumps(tables,ensure_ascii=False,indent=2),encoding='utf-8')
for t in tables:
    print(t['name']+': '+', '.join(r['name'] for r in c.execute('PRAGMA table_info('+t['name']+')').fetchall()))
c.close()
