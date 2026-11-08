"""个人主页服务入口：静态站托管 + 笔记发布 API，一个进程全包。

独立于零食店 Agent（不同仓库、不同进程、不同口令体系）。
本地：uvicorn app.main:app --port 8001
"""
from collections import defaultdict, deque
from time import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR
from app.notes_admin import router as notes_router
from app.proofread import router as proofread_router

app = FastAPI(title="Yubble 个人主页", docs_url=None, redoc_url=None)
app.include_router(notes_router)
app.include_router(proofread_router)


# ── 轻量限流 + 爆破封禁 ─────────────────────────────
# 公网上的写接口必须有闸门：/api/* 每 IP 每分钟 60 次；
# 口令 401 十次/10分钟 → 封 IP 10 分钟。
class _FailTracker:
    def __init__(self) -> None:
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._banned_until: dict[str, float] = {}

    def banned(self, ip: str) -> bool:
        until = self._banned_until.get(ip)
        if until and time() < until:
            return True
        if until:
            self._banned_until.pop(ip, None)
        return False

    def record(self, ip: str, max_fails: int = 10, window: float = 600.0, ban_s: float = 600.0) -> None:
        now = time()
        q = self._fails[ip]
        while q and now - q[0] > window:
            q.popleft()
        q.append(now)
        if len(q) >= max_fails:
            self._banned_until[ip] = now + ban_s
            q.clear()


_fails = _FailTracker()
_windows: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def _limit(request: Request, call_next) -> Response:
    ip = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api/"):
        if _fails.banned(ip):
            return JSONResponse({"detail": "鉴权失败次数过多，该地址已被临时封禁"}, status_code=429)
        now = time()
        q = _windows[ip]
        while q and now - q[0] > 60.0:
            q.popleft()
        q.append(now)
        if len(q) > 60:
            return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
        resp = await call_next(request)
        if resp.status_code in (401, 403):
            _fails.record(ip)
        return resp
    return await call_next(request)


# ── 静态站（site/ = index/works/notes + style.css）──
# html=True：/ → index.html。笔记页同源调用 /api/*，零跨域。
app.mount("/", StaticFiles(directory=ROOT_DIR / "site", html=True), name="site")
