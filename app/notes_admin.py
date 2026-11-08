"""笔记写作台后端（"Git 即 CMS"的提交通道）——个人主页独立服务。

静态站没法自己写 Git，由这个极小的服务代劳：
- 发布：把 md 写入 site/notes/ → git add + commit →（可选）push 到 GitHub
  发布即上线：本服务同时托管 site/ 静态文件，文件落盘访客立即可见，
  Git 提交负责留下"何时何文"的历史。
- 草稿存浏览器 localStorage（写作全程零口令、零请求），不经过服务器。

鉴权（独立于任何其他系统的口令）：
- NOTES_TOKEN：简单口令（如 zhiyong），只能写 site/notes/ 下的 md——
  风险半径是"有人冒名发笔记"，主人权衡后接受。带限流与爆破封禁。
- GET /source 免鉴权：笔记内容本来就是公开静态文件，没必要假装保密。

安全设计：
- 文件名白名单正则（YYYY-MM-DD-slug.md），杜绝路径穿越
- git 一律 argv 列表调用，无 shell 拼接；提交身份缺失时用 -c 参数兜底
- push 默认关闭（notes_git_push=false）：避免在无推送凭据的机器上反复失败
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import ROOT_DIR, get_settings

router = APIRouter(prefix="/api/admin/notes")

NOTES_DIR = ROOT_DIR / "site" / "notes"
NOTES_IMG_DIR = NOTES_DIR / "img"

# 文件名白名单：日期前缀 + 小写 slug。唯一允许进入仓库的文件名形状。
_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,80}\.md$")
MAX_CONTENT_CHARS = 200_000  # 20 万字符 ≈ 一篇超长技术文的十倍，防滥用
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _audit(event: str, target: str, detail: dict[str, Any]) -> None:
    """审计日志：独立服务没有共享数据库，落到进程日志（logs/app.log）。"""
    print(f"[audit] {event} {target} {json.dumps(detail, ensure_ascii=False)}")


def _clean_token(raw: str) -> str:
    """口令清洗：复原 HTTP 头的 latin-1 误解码 → NFKC 归一化 → 去空白。

    HTTP 头按 latin-1 解码，全角字母（UTF-8 字节）会变成 mojibake——
    先按 latin-1 还原字节再按 UTF-8 解码，之后 NFKC 才能把 ｚｈｉｙｏｎｇ 变回 zhiyong。
    """
    try:
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass  # 本来就是纯 ASCII 或无法复原，保持原样
    return unicodedata.normalize("NFKC", raw).strip()


def _require_notes_token(x_notes_token: str | None) -> None:
    """笔记口令校验：常量时间比较，防时序侧信道。

    输入先做 NFKC 归一化 + 去首尾空白：拼音输入法很容易打出全角字母
    （ｚｈｉｙｏｎｇ）——肉眼几乎无差别，字节完全不同。归一化后照常通过，
    免得主人在正确的口令上反复撞 401 还攒爆破计数。
    """
    expected = get_settings().notes_token
    if not expected:
        raise HTTPException(503, "服务端未配置 NOTES_TOKEN，笔记发布不可用")
    if not x_notes_token:
        print("[notes] 鉴权失败：请求未携带口令头（旧版页面或缺头）")
        raise HTTPException(401, "缺少笔记口令")
    given = _clean_token(x_notes_token)
    first = f"首字符 U+{ord(given[0]):04X}" if given else "内容为空白/不可见字符"
    # 用 bytes 比较：compare_digest 遇到非 ASCII 字符串会抛 TypeError，bytes 永不
    if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        hint = "（含全角或特殊字符，已自动转换仍不匹配）" if given != x_notes_token.strip() else ""
        # 401 提示带自诊断信息：长度 + 首字符码点，全角字母一眼识破
        raise HTTPException(401, f"笔记口令不对：收到 {len(given)} 字符、{first}{hint}")


def _safe_note_path(filename: str) -> Path:
    """校验文件名并返回笔记路径。任何越界尝试一律 400。"""
    if not _FILENAME_RE.match(filename):
        raise HTTPException(
            400,
            "文件名不符合约定：应为 YYYY-MM-DD-英文slug.md（如 2026-11-20-hybrid-retrieval.md），"
            "只允许小写字母、数字和短横线",
        )
    p = (NOTES_DIR / filename).resolve()
    if p.parent != NOTES_DIR.resolve():
        raise HTTPException(400, "非法路径")
    return p


def _extract_title(content: str) -> str:
    m = re.search(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
    return (m.group(1) if m else "无标题").strip()[:60]


def _rebuild_manifest() -> None:
    """重建笔记清单 index.json：列表页的主数据源（不依赖目录浏览）。

    随发布一起提交进 Git——任何静态服务器都能用，nginx 不用开 autoindex。
    """
    items = []
    for p in NOTES_DIR.glob("*.md"):
        if p.name.startswith("_"):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", p.name)
        items.append({
            "file": p.name,
            "title": _extract_title(p.read_text(encoding="utf-8")),
            "date": m.group(1) if m else "",
        })
    items.sort(key=lambda x: (x["date"], x["file"]), reverse=True)
    (NOTES_DIR / "index.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _looks_like_image(data: bytes) -> bool:
    """魔数判断：png / jpg / gif / webp（RIFF 容器，第 9-12 字节为 WEBP）。"""
    if len(data) < 12:
        return False
    if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8") or data.startswith(b"GIF8"):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _git(args: list[str], cwd: Path = ROOT_DIR) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60
    )


def _git_commit(notes_rel: str, title: str, action: str) -> dict[str, Any]:
    """git add 整个 notes 目录（含 img 里待提交的插图）+ commit。"""
    add = _git(["add", "--", notes_rel])
    if add.returncode != 0:
        raise HTTPException(500, f"git add 失败：{add.stderr.strip()[:200]}")

    # 无要提交的变化（内容与上次完全一致）→ 不造空提交
    status = _git(["status", "--porcelain", "--", notes_rel])
    if status.returncode == 0 and not status.stdout.strip():
        head = _git(["rev-parse", "--short", "HEAD"])
        return {"committed": False, "commit": head.stdout.strip() if head.returncode == 0 else "", "message": "内容与上次提交一致，无新提交"}

    identity = _git(["config", "user.email"])
    commit_args = ["commit", "-m", f"notes: {action}《{title}》"]
    if identity.returncode != 0 or not identity.stdout.strip():
        # 服务器仓库常缺身份配置：仅对本条提交生效，不改全局 gitconfig
        commit_args = ["-c", "user.name=Yubble", "-c", "user.email=notes@yubble.local", *commit_args]

    commit = _git(commit_args)
    if commit.returncode != 0:
        raise HTTPException(500, f"git commit 失败：{commit.stderr.strip()[:200]}")
    head = _git(["rev-parse", "--short", "HEAD"])
    return {"committed": True, "commit": head.stdout.strip(), "message": ""}


class PublishIn(BaseModel):
    filename: str
    content: str


@router.get("/source/{filename}")
def get_note_source(filename: str) -> dict[str, Any]:
    """读已发布笔记原文（编辑入口用）。内容本就是公开静态文件，免鉴权。"""
    p = _safe_note_path(filename)
    if not p.exists():
        raise HTTPException(404, "笔记不存在")
    return {"filename": filename, "content": p.read_text(encoding="utf-8")}


@router.post("/publish")
def publish_note(
    body: PublishIn,
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """发布：写文件 → git add/commit →（可选）push。文件落盘即上线。

    兼容两种头名：新版前端发 X-Notes-Token；缓存中的旧版页面发 X-Admin-Token。
    两个头都对照 NOTES_TOKEN 校验，值不对照样 401。
    """
    _require_notes_token(x_notes_token or x_admin_token)
    if not body.content.strip():
        raise HTTPException(400, "笔记内容为空")
    if len(body.content) > MAX_CONTENT_CHARS:
        raise HTTPException(400, f"内容超过 {MAX_CONTENT_CHARS} 字符上限")
    p = _safe_note_path(body.filename)
    is_update = p.exists()
    title = _extract_title(body.content)

    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    _rebuild_manifest()   # 清单随本次发布一起提交，列表页不依赖目录浏览

    result = _git_commit("site/notes", title, "更新" if is_update else "发布")

    pushed, push_msg = False, ""
    if get_settings().notes_git_push:
        # 推送前先同步远端：GitHub 可能被本地发布或其他机器推进过——
        # 不先 pull 必然 non-fast-forward 被拒（多端写作的真实场景）
        pre = _git(["pull", "--rebase", "--autostash", "origin", "main"])
        push = _git(["push", "origin", "HEAD"])
        pushed = push.returncode == 0
        push_msg = "" if pushed else (push.stderr.strip()[:160] or pre.stderr.strip()[:160])

    _audit(
        "notes.publish", body.filename,
        {"title": title, "update": is_update, "commit": result["commit"], "pushed": pushed},
    )
    return {
        "ok": True,
        "filename": body.filename,
        "update": is_update,
        "committed": result["committed"],
        "commit": result["commit"],
        "pushed": pushed,
        "push_msg": push_msg,
    }


@router.post("/delete/{filename}")
def delete_note(
    filename: str,
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """删除笔记：删文件 → 重建清单 → git 提交（与发布同一口令）。

    git add 对已删除文件同样生效（工作区没了 = 暂存区记录删除），
    历史里永远找得回——这正是 Git 即 CMS 比数据库 CMS 安心的地方。
    """
    _require_notes_token(x_notes_token or x_admin_token)
    p = _safe_note_path(filename)
    if not p.exists():
        raise HTTPException(404, "笔记不存在")
    title = _extract_title(p.read_text(encoding="utf-8"))
    p.unlink()
    _rebuild_manifest()
    result = _git_commit("site/notes", title, "删除")
    pushed, push_msg = False, ""
    if get_settings().notes_git_push:
        push = _git(["push", "origin", "HEAD"])
        pushed = push.returncode == 0
        push_msg = "" if pushed else push.stderr.strip()[:200]
    _audit("notes.delete", filename, {"title": title, "commit": result["commit"], "pushed": pushed})
    return {"ok": True, "filename": filename, "committed": result["committed"], "commit": result["commit"], "pushed": pushed}


@router.post("/image")
async def upload_note_image(
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """插图上传：存 notes/img/，返回 vditor 期望的响应形状（头名兼容同 publish）。

    插图不单独提交 git——随下一次笔记发布一起进仓库（_git_commit 提交整个 notes 目录）。
    """
    _require_notes_token(x_notes_token or x_admin_token)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(400, "只支持 png/jpg/jpeg/gif/webp 图片")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "图片超过 5MB 上限")
    if not data:
        raise HTTPException(400, "空文件")
    # 扩展名可能骗人，校验常见魔数（png/jpg/gif/webp），不认识的拒收
    if not _looks_like_image(data):
        raise HTTPException(400, "文件内容不是有效图片")
    NOTES_IMG_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4)}{ext}"
    (NOTES_IMG_DIR / name).write_bytes(data)
    _audit("notes.image", name, {"bytes": len(data)})
    return {
        "msg": "",
        "code": 0,
        "data": {"errFiles": [], "succMap": {file.filename or name: f"notes/img/{name}"}},
    }
