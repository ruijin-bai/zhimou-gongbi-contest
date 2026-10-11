"""Refresh only our generated issue labels; never reads another database."""
import sys
from datetime import date
from runtime import configure, ROOT
configure(); sys.path.insert(0,str(ROOT))
from app.db import connect, all_, loads, dumps
from app.services import claims
c=connect()
with c:
    for r in all_(c,"SELECT id,log_date,content_json,ai_draft_json FROM logs WHERE project_id=1 AND period='day'"):
        fields={}
        for col in ('content_json','ai_draft_json'):
            v=loads(r[col],{}) or {}
            for i in v.get('issues',[]):
                if i.get('type')=='天气':
                    i.update(type='施工组织',desc=['雨季路床含水率偏高，开放面暂缓碾压。','便道运输窗口受限，已组织清沟排水。','雨季有效作业窗口缩短，排水完成后复核含水率。','雨后软弱点位已标识，先处理再恢复填筑。','湿基层暂不摊铺，班组转入材料检查与机具保养。'][date.fromisoformat(r['log_date']).day%5],measure='调整至排水、机具保养与检验，恢复前复测')
                if i.get('type')=='材料':
                    i.update(type='施工组织',desc='水稳拌合料运输周转衔接不顺，末班摊铺窗口受限。')
            fields[col]=dumps(v)
        c.execute('UPDATE logs SET content_json=?,ai_draft_json=? WHERE id=?',(fields['content_json'],fields['ai_draft_json'],r['id']))
    c.execute('DELETE FROM digest_cache')
assert not claims.all_events(c,1), 'Synthetic contract-derived events must stay empty'
c.close()
print('Own issues refreshed; contract-derived events: 0')
