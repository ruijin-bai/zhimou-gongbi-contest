"""现场圈 Agent 化：Agent 同事点评、@工笔、接力时间线、AI精选、公告与已读回执。

动态本身仍由 services/feed.py 从既有记录汇总；这里只新增几张表和一组 /api/circle/* 接口。
权限一律后端强制：现场圈全项目可见（10-09），领导只读，系统管理员不参与；公告发布仅港湾管理岗。
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_agent_notes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    perspective TEXT NOT NULL,
    text TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'rule',
    ai_run_id INTEGER,
    audience TEXT NOT NULL DEFAULT 'all',
    asked_by INTEGER,
    question TEXT,
    reply_to INTEGER,
    scope TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_feed_agent_view ON feed_agent_notes(project_id, post_key, perspective) WHERE perspective<>'qa';
CREATE INDEX IF NOT EXISTS ix_feed_agent_post ON feed_agent_notes(project_id, post_key);
CREATE TABLE IF NOT EXISTS circle_notices (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    audience TEXT NOT NULL DEFAULT 'all',
    company TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_circle_notices ON circle_notices(project_id, status, created_at);
CREATE TABLE IF NOT EXISTS circle_notice_reads (
    notice_id INTEGER NOT NULL REFERENCES circle_notices(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    read_at TEXT NOT NULL,
    PRIMARY KEY (notice_id, user_id)
);
CREATE TABLE IF NOT EXISTS circle_picks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'rule',
    ai_run_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, post_key)
);
"""


def register(app) -> None:
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel, Field

    from .. import main as M
    from ..services import circle_board as board
    from ..services import circle_picks as picks
    from ..services import features as feat_svc
    from ..services import feed_agents as agents

    def writer(user: dict = Depends(M.current_user)) -> dict:
        """能参与现场圈互动的角色：系统管理员不参与，领导只读。"""
        if user["role"] == "admin":
            raise HTTPException(403, "系统管理员不参与现场动态")
        if user["role"] == "leader":
            raise HTTPException(403, "当前角色只能查看现场动态")
        return user

    def viewer(user: dict = Depends(M.current_user)) -> dict:
        if user["role"] == "admin":
            raise HTTPException(403, "系统管理员不参与现场动态")
        return user

    def manager(user: dict = Depends(M.current_user)) -> dict:
        if not board.can_publish(user):
            raise HTTPException(403, "只有港湾管理岗（项目经理、工程主管、安全主管）可以操作")
        return user

    class ReviewIn(BaseModel):
        keys: list[str] = Field(max_length=30)

    class AskIn(BaseModel):
        key: str = Field(max_length=40)
        text: str = Field(max_length=400)

    class NoticeIn(BaseModel):
        title: str = Field(max_length=80)
        body: str = Field(max_length=1000)
        pinned: bool = False
        audience: str = "all"
        company: str = Field("", max_length=60)

    class PinIn(BaseModel):
        pinned: bool

    @app.get("/api/circle/meta")
    def circle_meta(user=Depends(viewer), conn=Depends(M.db)):
        pub = board.can_publish(user)
        return {"can_publish": pub, "companies": board.companies(conn, user["project_id"]) if pub else [],
                "can_write": user["role"] != "leader"}

    @app.post("/api/circle/review")
    def circle_review(body: ReviewIn, user=Depends(viewer), conn=Depends(M.db)):
        """打开现场圈后异步调用：补 Agent 点评；顺带让 Agent 从近 7 天的真实动态里挑 AI精选（节流：同项目 10 分钟内最多试一次，每天最多自动一轮）。"""
        if not feat_svc.enabled(conn, user["project_id"], "circle_agent"):   # 项目设置里隐藏了“现场圈 Agent”：不再生成点评与精选
            return {"comments": {}, "left": [], "picked": False}
        comments, left = agents.review(conn, user, body.keys)
        picked = picks.maybe_auto_pick(conn, user["project_id"])
        return {"comments": comments, "left": left, "picked": bool(picked)}

    @app.get("/api/circle/relay")
    def circle_relay(key: str, user=Depends(viewer), conn=Depends(M.db)):
        return {"key": key, "steps": agents.relay(conn, user, key)}

    @app.post("/api/circle/ask")
    def circle_ask(body: AskIn, user=Depends(writer), conn=Depends(M.db)):
        return agents.ask(conn, user, body.key, body.text)

    @app.delete("/api/circle/agent-comments/{raw_id}")
    def circle_agent_comment_del(raw_id: int, user=Depends(writer), conn=Depends(M.db)):
        M._guard(agents.delete_answer, conn, user, raw_id)
        return {"ok": True}

    # ------------------------------------------------------------------ 公告
    @app.post("/api/circle/notices")
    def notice_publish(body: NoticeIn, user=Depends(manager), conn=Depends(M.db)):
        n = M._guard(board.publish, conn, user, body.title, body.body, body.pinned, body.audience, body.company)
        return {"key": f"notice:{n['id']}", "notice": board.notice_view(conn, user, n)}

    @app.post("/api/circle/notices/{nid}/read")
    def notice_read(nid: int, user=Depends(viewer), conn=Depends(M.db)):
        return board.mark_read(conn, user, nid)

    @app.get("/api/circle/notices/{nid}/reads")
    def notice_reads(nid: int, user=Depends(viewer), conn=Depends(M.db)):
        return M._guard(board.reads, conn, user, nid)

    @app.post("/api/circle/notices/{nid}/pin")
    def notice_pin(nid: int, body: PinIn, user=Depends(manager), conn=Depends(M.db)):
        return M._guard(board.set_pinned, conn, user, nid, body.pinned)

    @app.delete("/api/circle/notices/{nid}")
    def notice_withdraw(nid: int, user=Depends(manager), conn=Depends(M.db)):
        return M._guard(board.withdraw, conn, user, nid)
