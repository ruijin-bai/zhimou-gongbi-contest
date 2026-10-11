"""Logical fingerprint of our seed data, without exporting or printing row contents."""
import hashlib,json,sqlite3
from runtime import DATA
c=sqlite3.connect('file:'+str(DATA/'zhimou.db')+'?mode=ro',uri=True)
tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
h=hashlib.sha256()
for t in tables:
    h.update(t.encode())
    rows=sorted(json.dumps(list(r),ensure_ascii=False,separators=(',',':')) for r in c.execute('SELECT * FROM "'+t+'"'))
    for r in rows:h.update(r.encode())
c.close()
print(h.hexdigest())
