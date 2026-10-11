"""现场圈讨论组接口（/api/talk/*）：话题 / 问题 / 专题三类小组，组内发帖、赞、评论，关联工点或作业，关闭与置顶。规则见 services/talk.py。

- GET    /api/talk/options                     建组可选的类型、工点、作业
- GET    /api/talk/groups?kind=&status=&q=     小组列表（置顶在前，按最近活跃）
- POST   /api/talk/groups                      建组 {kind, title, intro, part, activity_code}
- GET    /api/talk/groups/{gid}?before=        小组详情与帖子（分页）
- POST   /api/talk/groups/{gid}/status         关闭 / 重新打开 {closed, note}
- POST   /api/talk/groups/{gid}/pin            置顶 {pinned}（港湾管理岗）
- POST   /api/talk/groups/{gid}/posts          发帖 {text, media_ids}
- DELETE /api/talk/posts/{pid}                 删自己的帖子
- POST   /api/talk/posts/{pid}/like            赞 / 取消
- POST   /api/talk/posts/{pid}/comments        评论 {text, reply_to}
- DELETE /api/talk/comments/{cid}              删自己的评论
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS talk_groups (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    intro TEXT NOT NULL DEFAULT '',
    part TEXT NOT NULL DEFAULT '',
    activity_code TEXT NOT NULL DEFAULT '',
    author_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open',
    pinned INTEGER NOT NULL DEFAULT 0,
    closed_by INTEGER,
    closed_at TEXT,
    close_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_talk_groups ON talk_groups(project_id, status, pinned, last_at);
CREATE TABLE IF NOT EXISTS talk_posts (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL REFERENCES talk_groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_talk_posts ON talk_posts(group_id, id);
CREATE TABLE IF NOT EXISTS talk_comments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL REFERENCES talk_posts(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    reply_to INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_talk_comments ON talk_comments(post_id, id);
CREATE TABLE IF NOT EXISTS talk_likes (
    post_id INTEGER NOT NULL REFERENCES talk_posts(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id)
);
"""


def register(app) -> None:
    from fastapi import Depends
    from pydantic import BaseModel, Field

    from .. import main as M
    from ..services import talk as svc

    class GroupIn(BaseModel):
        kind: str = Field(max_length=10)
        title: str = Field(max_length=80)
        intro: str = Field("", max_length=600)
        part: str = Field("", max_length=60)
        activity_code: str = Field("", max_length=40)

    class StatusIn(BaseModel):
        closed: bool
        note: str = Field("", max_length=400)

    class PinIn(BaseModel):
        pinned: bool

    class PostIn(BaseModel):
        text: str = Field("", max_length=2000)
        media_ids: list[int] = Field(default_factory=list, max_length=9)

    class CommentIn(BaseModel):
        text: str = Field(max_length=600)
        reply_to: int | None = None

    g = M._guard

    @app.get("/api/talk/options")
    def talk_options(user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.options, conn, user)

    @app.get("/api/talk/groups")
    def talk_groups(kind: str = "", status: str = "open", q: str = "", user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.list_groups, conn, user, kind, status, q)

    @app.post("/api/talk/groups")
    def talk_create(body: GroupIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.create_group, conn, user, body.kind, body.title, body.intro, body.part, body.activity_code)

    @app.get("/api/talk/groups/{gid}")
    def talk_detail(gid: int, before: int | None = None, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.detail, conn, user, gid, before)

    @app.post("/api/talk/groups/{gid}/status")
    def talk_status(gid: int, body: StatusIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.set_status, conn, user, gid, body.closed, body.note)

    @app.post("/api/talk/groups/{gid}/pin")
    def talk_pin(gid: int, body: PinIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.set_pinned, conn, user, gid, body.pinned)

    @app.post("/api/talk/groups/{gid}/posts")
    def talk_post(gid: int, body: PostIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.add_post, conn, user, gid, body.text, body.media_ids)

    @app.delete("/api/talk/posts/{pid}")
    def talk_post_del(pid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        g(svc.delete_post, conn, user, pid)
        return {"ok": True}

    @app.post("/api/talk/posts/{pid}/like")
    def talk_like(pid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.toggle_like, conn, user, pid)

    @app.post("/api/talk/posts/{pid}/comments")
    def talk_comment(pid: int, body: CommentIn, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.add_comment, conn, user, pid, body.text, body.reply_to)

    @app.delete("/api/talk/comments/{cid}")
    def talk_comment_del(cid: int, user=Depends(M.current_user), conn=Depends(M.db)):
        return g(svc.delete_comment, conn, user, cid)
