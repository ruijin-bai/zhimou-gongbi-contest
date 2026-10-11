"""现场圈社交层接口（/api/social/*）：收藏、关注/粉丝、主页、转发、私信，以及简报阅读（AI 总结、划重点、精选评论）。

权限：系统管理员不参与；公司领导只读（可收藏、可私下划重点，不能关注、转发、私信、评论）；其余岗位可参与。
私信只有会话双方可读：没有管理员查阅入口，也没有删除/撤回接口。
"""
from fastapi import BackgroundTasks, File, UploadFile

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_favs (
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, post_key, user_id)
);
CREATE INDEX IF NOT EXISTS ix_feed_favs_user ON feed_favs(project_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS feed_follows (
    project_id INTEGER NOT NULL,
    follower_id INTEGER NOT NULL REFERENCES users(id),
    followee_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, follower_id, followee_id)
);
CREATE INDEX IF NOT EXISTS ix_feed_follows_to ON feed_follows(project_id, followee_id);
CREATE TABLE IF NOT EXISTS feed_reposts (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    orig_key TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feed_reposts_orig ON feed_reposts(project_id, orig_key);
CREATE TABLE IF NOT EXISTS feed_highlights (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    para TEXT NOT NULL,
    quote TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    public INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, post_key, user_id, para, quote)
);
CREATE INDEX IF NOT EXISTS ix_feed_hl_post ON feed_highlights(project_id, post_key);
CREATE TABLE IF NOT EXISTS feed_reads (
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    read_at TEXT NOT NULL,
    PRIMARY KEY (project_id, post_key, user_id)
);
CREATE TABLE IF NOT EXISTS feed_summaries (
    project_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    body_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'rule',
    ai_run_id INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, post_key)
);
CREATE TABLE IF NOT EXISTS dm_threads (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    user_lo INTEGER NOT NULL REFERENCES users(id),
    user_hi INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    UNIQUE (project_id, user_lo, user_hi)
);
CREATE TABLE IF NOT EXISTS dm_messages (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES dm_threads(id),
    project_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]',
    ref_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dm_msg_thread ON dm_messages(thread_id, id);
CREATE TABLE IF NOT EXISTS dm_reads (
    thread_id INTEGER NOT NULL REFERENCES dm_threads(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    last_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (thread_id, user_id)
);
CREATE TABLE IF NOT EXISTS dm_files (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    file TEXT NOT NULL,
    thumb TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def migrate(conn) -> None:
    """评论增加：回复谁、对哪句话评论（引用）、精选。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_comments)").fetchall()}
    for name, ddl in (("reply_to", "INTEGER"), ("quote", "TEXT NOT NULL DEFAULT ''"), ("featured", "INTEGER NOT NULL DEFAULT 0")):
        if cols and name not in cols:
            conn.execute(f"ALTER TABLE feed_comments ADD COLUMN {name} {ddl}")


def register(app) -> None:
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field

    from .. import main as M
    from ..services import social as svc

    def viewer(user: dict = Depends(M.current_user)) -> dict:
        if user["role"] == "admin":
            raise HTTPException(403, "系统管理员不参与现场动态")
        return user

    def writer(user: dict = Depends(viewer)) -> dict:
        if user["role"] == "leader":
            raise HTTPException(403, "当前角色只能查看现场动态")
        return user

    def social_on(user: dict = Depends(viewer), conn=Depends(M.db)) -> dict:
        """社交写操作的总开关：默认关闭，管理员在“项目与规则”开启后才放行。"""
        if not svc.enabled(conn, user["project_id"]):
            raise HTTPException(403, "功能未开启：现场圈社交层默认关闭，需系统管理员在“项目与规则”中开启")
        return user

    def social_write(user: dict = Depends(social_on)) -> dict:
        if user["role"] == "leader":
            raise HTTPException(403, "当前角色只能查看现场动态")
        return user

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    class KeyIn(BaseModel):
        key: str = Field(max_length=40)

    class FollowIn(BaseModel):
        user_id: int
        on: bool = True

    class RepostIn(BaseModel):
        key: str = Field(max_length=40)
        text: str = Field("", max_length=400)

    class FeatureIn(BaseModel):
        featured: bool = True

    class HighlightIn(BaseModel):
        key: str = Field(max_length=40)
        para: str = Field(max_length=8)
        quote: str = Field(max_length=300)
        note: str = Field("", max_length=200)
        public: bool = False

    class HighlightPut(BaseModel):
        public: bool | None = None
        note: str | None = Field(None, max_length=200)

    class SummaryIn(BaseModel):
        key: str = Field(max_length=40)
        force: bool = False

    class DmIn(BaseModel):
        to: int
        text: str = Field("", max_length=1200)
        file_ids: list[int] = []
        ref_key: str | None = Field(None, max_length=40)

    # ------------------------------------------------------------------ 人、主页、关注
    @app.get("/api/social/people")
    def social_people(user=Depends(viewer), conn=Depends(M.db)):
        return svc.people(conn, user)

    @app.get("/api/social/users/{uid}")
    def social_profile(uid: int, user=Depends(viewer), conn=Depends(M.db)):
        return guard(svc.profile, conn, user, uid)

    @app.get("/api/social/users/{uid}/follows")
    def social_follows(uid: int, type: str = "following", user=Depends(viewer), conn=Depends(M.db)):
        if not svc.enabled(conn, user["project_id"]):
            return []
        return guard(svc.follow_list, conn, user, uid, "followers" if type == "followers" else "following")

    @app.post("/api/social/follow")
    def social_follow(body: FollowIn, user=Depends(social_write), conn=Depends(M.db)):
        return guard(svc.set_follow, conn, user, body.user_id, body.on)

    # ------------------------------------------------------------------ 收藏、转发、精选
    @app.post("/api/social/fav")
    def social_fav(body: KeyIn, user=Depends(social_on), conn=Depends(M.db)):
        return guard(svc.toggle_fav, conn, user, body.key)

    @app.post("/api/social/repost")
    def social_repost(body: RepostIn, user=Depends(social_write), conn=Depends(M.db)):
        return guard(svc.repost, conn, user, body.key, body.text)

    @app.post("/api/social/comments/{cid}/feature")
    def social_feature(cid: int, body: FeatureIn, user=Depends(social_on), conn=Depends(M.db)):
        return guard(svc.feature_comment, conn, user, cid, body.featured)

    # ------------------------------------------------------------------ 阅读、总结、划重点
    @app.get("/api/social/article")
    def social_article(key: str, user=Depends(viewer), conn=Depends(M.db)):
        return guard(svc.article, conn, user, key)

    @app.post("/api/social/article/summary")
    def social_summary(body: SummaryIn, bg: BackgroundTasks, user=Depends(social_on), conn=Depends(M.db)):
        return guard(svc.summarize, conn, user, body.key, body.force, bg)

    @app.get("/api/social/article/summary")
    def social_summary_state(key: str, user=Depends(viewer), conn=Depends(M.db)):
        return guard(svc.summary_state, conn, user, key)

    @app.post("/api/social/highlights")
    def social_hl_add(body: HighlightIn, user=Depends(social_on), conn=Depends(M.db)):
        return guard(svc.add_highlight, conn, user, body.key, body.para, body.quote, body.note, body.public)

    @app.put("/api/social/highlights/{hid}")
    def social_hl_put(hid: int, body: HighlightPut, user=Depends(social_on), conn=Depends(M.db)):
        return guard(svc.set_highlight, conn, user, hid, body.public, body.note)

    @app.delete("/api/social/highlights/{hid}")
    def social_hl_del(hid: int, user=Depends(social_on), conn=Depends(M.db)):
        guard(svc.delete_highlight, conn, user, hid)
        return {"ok": True}

    @app.get("/api/social/highlights/mine")
    def social_hl_mine(user=Depends(viewer), conn=Depends(M.db)):
        if not svc.enabled(conn, user["project_id"]):
            return {"items": []}
        return {"items": svc.my_highlights(conn, user)}

    # ------------------------------------------------------------------ 私信（仅会话双方可读；无删除、无撤回、无管理员入口）
    @app.get("/api/social/summary")
    def social_summary_counts(user=Depends(viewer), conn=Depends(M.db)):
        if not svc.enabled(conn, user["project_id"]):
            return {"dm_unread": 0}
        return {"dm_unread": svc.dm_unread(conn, user)}

    @app.get("/api/social/dm/threads")
    def dm_threads(user=Depends(writer), conn=Depends(M.db)):
        if not svc.enabled(conn, user["project_id"]):
            return {"threads": []}
        return {"threads": guard(svc.dm_threads, conn, user)}

    @app.get("/api/social/dm/with/{peer}")
    def dm_with(peer: int, after: int = 0, mark: int = 1, user=Depends(social_write), conn=Depends(M.db)):
        return guard(svc.dm_messages, conn, user, peer, after, bool(mark))

    @app.post("/api/social/dm/send")
    def dm_send(body: DmIn, user=Depends(social_write), conn=Depends(M.db)):
        return guard(svc.dm_send, conn, user, body.to, body.text, body.file_ids, body.ref_key)

    @app.post("/api/social/dm/photo")
    def dm_photo(files: list[UploadFile] = File(...), user=Depends(social_write), conn=Depends(M.db)):
        if len(files) > 9:
            raise HTTPException(400, "一次最多 9 张")
        return [guard(svc.dm_save_file, conn, user, f.filename or "", f.file.read(svc.DM_MAX_UPLOAD + 1)) for f in files]

    @app.get("/api/social/dm/files/{fid}/{which}")
    def dm_file(fid: int, which: str, user=Depends(social_write), conn=Depends(M.db)):
        path = svc.dm_file_path(conn, user, fid, "thumb" if which == "thumb" else "file")
        if not path:
            raise HTTPException(404, "照片不存在")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
