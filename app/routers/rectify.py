"""整改复核（T-13）：AI 复核只给建议，闭环 / 退回由人确认。

- `POST /api/findings/{id}/reverify`：重新让 AI 复核。只限安全主管、项目经理；只在 AI 复核失败，或进行中超过 10 分钟时可用，
  其余情况返回 409；写审计。这是除首次复核外唯一允许再次调用 AI 的途径。
- 整改照片拍摄时间早于派单超过 24 小时（`site_agents.RectifyPhotoError`）：凡是提交整改的接口统一返回
  422 `{"detail", "code": "photo_before_dispatch", "media_id"}`，照片仍保存在服务器上，只是不挂到这条整改。
"""
from __future__ import annotations

from fastapi import BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse


def register(app) -> None:
    from .. import main as M
    from ..services import site_agents as svc

    async def _photo_error(_, exc: svc.RectifyPhotoError):
        return JSONResponse({"detail": str(exc), "code": exc.code, "media_id": exc.media_id}, status_code=422)

    app.add_exception_handler(svc.RectifyPhotoError, _photo_error)

    @app.post("/api/findings/{fid}/reverify")
    def finding_reverify(fid: int, bg: BackgroundTasks, user=Depends(M.current_user), conn=Depends(M.db)):
        """重新让 AI 复核：重新入队，复核在响应发出之后执行。"""
        try:
            return svc.rectify_reverify(conn, fid, user, bg)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except svc.VerifyConflict as exc:
            raise HTTPException(409, str(exc)) from None
