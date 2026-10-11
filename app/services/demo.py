"""竞赛演示账号：一个账号登录一次，就能在项目经理、工程主管、安全主管三个岗位之间一键切换，录视频不用退出重登。

- 只对 `.env` 里 `ZB_DEMO_USERS`（逗号分隔的用户名）列出的账号生效；比赛结束删掉这一项，演示功能整体消失，账号回到自己的本岗位；
- 只能在三个港湾管理岗之间切换，切不到系统管理员、领导或分包岗位；账号本身也须是这三个岗位之一；
- 切换后整个请求按所选岗位处理（权限、导航、首页、项目经理口径守卫都照原样生效），不另开后门；
- 当前岗位存在 cookie `zb_as` 里；每次切换写审计（demo.as），留痕可查。
"""
from __future__ import annotations

import os

from .. import roles as R

DEMO_ROLES = ("manager", "eng_sup", "safety_sup")
COOKIE = "zb_as"


def demo_users() -> set[str]:
    return {u.strip() for u in os.environ.get("ZB_DEMO_USERS", "").split(",") if u.strip()}


def is_demo(user: dict) -> bool:
    return user["username"] in demo_users() and user["role"] in DEMO_ROLES


def act_as(user: dict, cookies) -> dict:
    """current_user 的出口：演示账号按 cookie 里选的岗位返回（real_role 记本来岗位）；其他账号原样返回。"""
    if not is_demo(user):
        return user
    want = cookies.get(COOKIE) or ""
    return {**user, "role": want if want in DEMO_ROLES else user["role"], "real_role": user["role"], "demo": True}


def info(user: dict) -> dict | None:
    """/api/me 用：演示账号返回可切换的岗位与当前岗位；其他账号返回 None（前端不显示切换条）。"""
    if not user.get("demo"):
        return None
    return {"as": user["role"], "roles": [{"key": r, "name": R.role_name(r)} for r in DEMO_ROLES]}
