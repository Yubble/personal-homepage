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
# 目录名白名单（2026-11-24 目录功能）：中文/字母/数字/下划线/短横线，1-30 字符。
# 不含斜杠、点、空格——物理目录 site/notes/<目录名>/，URL 全程可安全编码；
# ".." 因点号被排除天然防穿越，无需单独判断。
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff\u3400-\u4dbf_\-]{1,30}$")
MAX_CONTENT_CHARS = 200_000  # 20 万字符 ≈ 一篇超长技术文的十倍，防滥用
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _clean_category(raw: str | None) -> str:
    """目录名清洗：NFKC 归一（全角字母→半角，同口令清洗的理由）→ 去首尾空白与斜杠。

    空串 = 未分类（笔记留在 notes/ 根目录，兼容历史行为）。
    """
    if not raw:
        return ""
    c = unicodedata.normalize("NFKC", raw).strip().strip("/")
    if c and not _CATEGORY_RE.match(c):
        raise HTTPException(
            400,
            "目录名只允许中文、字母、数字、短横线或下划线（1-30 字符），"
            "不允许空格、斜杠和点号（如：FDE实践 / AI技术 / 商业思考）",
        )
    return c


def _split_rel_path(file: str) -> tuple[str, str]:
    """相对路径拆分：'FDE实践/2026-11-20-x.md' → ('FDE实践', '2026-11-20-x.md')。

    接受 0/1 段目录；来源接口（source/delete）的 path 参数。
    """
    rel = (file or "").strip().strip("/")
    parts = [s for s in rel.split("/") if s]
    if not parts:
        raise HTTPException(400, "缺少文件路径")
    if len(parts) > 2:
        raise HTTPException(400, "路径最多一层目录：目录/文件名.md")
    category = _clean_category(parts[0]) if len(parts) == 2 else ""
    return category, parts[-1]


def _note_rel_path(p: Path) -> str:
    """文件路径 → 相对 notes/ 的 posix 字符串（清单 file 字段与前端路由的统一形状）。"""
    return p.resolve().relative_to(NOTES_DIR.resolve()).as_posix()


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


def _safe_note_path(filename: str, category: str = "") -> Path:
    """校验文件名（+可选目录名）并返回笔记路径。任何越界尝试一律 400。

    目录为空 = 未分类（notes/ 根目录，历史行为）；有目录 = notes/<目录>/。
    白名单不含点号和斜杠，加上 resolve 后必须在 NOTES_DIR 之下的双重校验，
    路径穿越（../、绝对路径、符号链接逃逸）全部拦截。
    """
    if not _FILENAME_RE.match(filename):
        raise HTTPException(
            400,
            "文件名不符合约定：应为 YYYY-MM-DD-英文slug.md（如 2026-11-20-hybrid-retrieval.md），"
            "只允许小写字母、数字和短横线",
        )
    if category and not _CATEGORY_RE.match(category):
        raise HTTPException(400, "非法目录名")
    p = (NOTES_DIR / category / filename if category else NOTES_DIR / filename).resolve()
    root = NOTES_DIR.resolve()
    if root not in p.parents:
        raise HTTPException(400, "非法路径")
    if NOTES_IMG_DIR.resolve() in p.parents:
        raise HTTPException(400, "非法路径")
    return p


def _extract_title(content: str) -> str:
    m = re.search(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
    return (m.group(1) if m else "无标题").strip()[:60]


def _rebuild_manifest() -> None:
    """重建笔记清单 index.json：列表页的主数据源（不依赖目录浏览）。

    2026-11-24 起支持目录：递归扫 notes/ 下所有子目录，每条带 category 字段
    （file 为相对路径如 'FDE实践/2026-11-20-x.md'；根目录旧笔记 category=""
    视为未分类——老清单没有该字段，前端按空值兼容，双向平滑）。
    随发布一起提交进 Git——任何静态服务器都能用，nginx 不用开 autoindex。
    """
    items = []
    for p in NOTES_DIR.rglob("*.md"):
        if p.name.startswith("_"):
            continue
        rel = _note_rel_path(p)
        if rel.startswith("img/"):
            continue  # 插图目录防御：不该有 md，真有也不进清单
        parts = rel.split("/")
        category = parts[0] if len(parts) > 1 else ""
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", p.name)
        items.append({
            "file": rel,
            "title": _extract_title(p.read_text(encoding="utf-8")),
            "date": m.group(1) if m else "",
            "category": category,
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


def _heal_manifest_rebase_conflict() -> bool:
    """rebase 停在 index.json 冲突时自动合并两端条目并标记解决。

    合并规则：两 stage 条目按 file 字段去重取并集、date 降序（新笔记在前，
    与清单既有顺序一致）。只在"唯一冲突文件就是清单"时自动处理——
    出现任何其他冲突文件说明是实质内容分叉，不敢替人做主，返回 False
    保留原报错。2026-11-17 案例：服务器与 GitHub 双头各推了笔记提交，
    pull --rebase 停在清单冲突，push 报 non-fast-forward 给到前端。
    """
    if not ((ROOT_DIR / ".git/rebase-merge").exists() or (ROOT_DIR / ".git/rebase-apply").exists()):
        return False   # 不在 rebase 冲突状态，无自愈对象
    unstaged = _git(["diff", "--name-only", "--diff-filter=U"]).stdout.split()
    if unstaged != ["site/notes/index.json"]:
        return False   # 有其他冲突文件（或不是清单），不自动处理
    def _stage(n: int) -> list:
        raw = _git(["show", f":{n}:site/notes/index.json"]).stdout
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []
    ours, theirs = _stage(2), _stage(3)
    seen = {i.get("file") for i in ours if isinstance(i, dict)}
    merged = ours + [i for i in theirs if isinstance(i, dict) and i.get("file") not in seen]
    merged.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    (ROOT_DIR / "site/notes/index.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _git(["add", "--", "site/notes/index.json"])
    return True


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
    category: str = ""    # 发布/移动目标目录（空 = 未分类根目录）
    old_file: str = ""    # 编辑已有笔记时的原相对路径——与目标目录不同即"移动"


@router.get("/source/{file:path}")
def get_note_source(file: str) -> dict[str, Any]:
    """读已发布笔记原文（编辑入口用）。内容本就是公开静态文件，免鉴权。

    file 为相对 notes/ 的路径：'2026-...md'（未分类）或 '目录名/2026-...md'。
    老版页面传纯文件名 → 根目录，向后兼容。
    """
    category, filename = _split_rel_path(file)
    p = _safe_note_path(filename, category)
    if not p.exists():
        raise HTTPException(404, "笔记不存在")
    return {"filename": _note_rel_path(p), "content": p.read_text(encoding="utf-8")}


@router.post("/publish")
def publish_note(
    body: PublishIn,
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """发布：写文件 → git add/commit →（可选）push。文件落盘即上线。

    兼容两种头名：新版前端发 X-Notes-Token；缓存中的旧版页面发 X-Admin-Token。
    两个头都对照 NOTES_TOKEN 校验，值不对照样 401。

    目录支持（2026-11-24）：category 为空落在 notes/ 根（未分类）；带目录则
    写入 notes/<目录>/。编辑时换目录：old_file 指向原位置，写新 + 删旧在
    同一次 git 提交里原子完成（Git 记录为 rename，历史可追溯）。
    旧版前端不发这两个新字段 → category 空 + old_file 空，行为与从前一致。
    """
    _require_notes_token(x_notes_token or x_admin_token)
    if not body.content.strip():
        raise HTTPException(400, "笔记内容为空")
    if len(body.content) > MAX_CONTENT_CHARS:
        raise HTTPException(400, f"内容超过 {MAX_CONTENT_CHARS} 字符上限")
    category = _clean_category(body.category)
    p = _safe_note_path(body.filename, category)

    moved = False
    old_rel = (body.old_file or "").strip().strip("/")
    if old_rel:
        o_category, o_filename = _split_rel_path(old_rel)
        old_path = _safe_note_path(o_filename, o_category)
        if old_path.exists() and old_path != p:
            moved = True   # 先写新文件再删旧：任何一步失败内容都不丢

    is_update = p.exists() or moved
    title = _extract_title(body.content)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    if moved:
        old_path.unlink()
    _rebuild_manifest()   # 清单随本次发布一起提交，列表页不依赖目录浏览

    action = "移动" if moved else ("更新" if is_update else "发布")
    result = _git_commit("site/notes", title, action)

    pushed, push_msg = False, ""
    if get_settings().notes_git_push:
        # 推送前先同步远端：GitHub 可能被本地发布或其他机器推进过——
        # 不先 pull 必然 non-fast-forward 被拒（多端写作的真实场景）。
        # 2026-11-17 实测：pull --rebase 会在 index.json 上冲突（两端都往
        # 清单顶部插条目），pull 停在冲突状态后 push 照旧被拒，报错透传给
        # 用户——所以自愈还必须能自动合并清单：两 stage 条目按 file 去重、
        # 日期降序，continue 后再推（最多 3 轮，防连续多提交都冲突）。
        _git(["pull", "--rebase", "--autostash", "origin", "main"])
        push = _git(["push", "origin", "HEAD"])
        pushed = push.returncode == 0
        for _ in range(3):
            if pushed or not _heal_manifest_rebase_conflict():
                break
            _git(["-c", "core.editor=true", "rebase", "--continue"])
            push = _git(["push", "origin", "HEAD"])
            pushed = push.returncode == 0
        push_msg = "" if pushed else (push.stderr.strip()[:160])

    _audit(
        "notes.publish", _note_rel_path(p),
        {"title": title, "update": is_update, "moved": moved, "category": category,
         "commit": result["commit"], "pushed": pushed},
    )
    return {
        "ok": True,
        "filename": _note_rel_path(p),
        "update": is_update,
        "committed": result["committed"],
        "commit": result["commit"],
        "pushed": pushed,
        "push_msg": push_msg,
    }


@router.post("/delete/{file:path}")
def delete_note(
    file: str,
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """删除笔记：删文件 → 重建清单 → git 提交（与发布同一口令）。

    file 为相对 notes/ 的路径（'目录名/2026-...md' 或纯文件名），兼容老版页面。
    git add 对已删除文件同样生效（工作区没了 = 暂存区记录删除），
    历史里永远找得回——这正是 Git 即 CMS 比数据库 CMS 安心的地方。
    """
    _require_notes_token(x_notes_token or x_admin_token)
    category, filename = _split_rel_path(file)
    p = _safe_note_path(filename, category)
    if not p.exists():
        raise HTTPException(404, "笔记不存在")
    rel = _note_rel_path(p)
    title = _extract_title(p.read_text(encoding="utf-8"))
    p.unlink()
    _rebuild_manifest()
    result = _git_commit("site/notes", title, "删除")
    pushed, push_msg = False, ""
    if get_settings().notes_git_push:
        push = _git(["push", "origin", "HEAD"])
        pushed = push.returncode == 0
        push_msg = "" if pushed else push.stderr.strip()[:200]
    _audit("notes.delete", rel, {"title": title, "commit": result["commit"], "pushed": pushed})
    return {"ok": True, "filename": rel, "committed": result["committed"], "commit": result["commit"], "pushed": pushed}


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
