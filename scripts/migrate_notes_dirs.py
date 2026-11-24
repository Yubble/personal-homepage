"""一次性迁移：把 notes/ 根目录的存量笔记按主题归入子目录（2026-11-24 目录功能）。

在部署了目录功能的服务器容器里执行一次（整仓库挂载，脚本在宿主仓库内可见）：
    docker exec yubble-homepage python scripts/migrate_notes_dirs.py
幂等：已在子目录的笔记不动；根目录没有笔记时空跑退出。
分类规则（按标题关键词，简单可预测；归错了在编辑器里改目录重新发布即移动）：
    标题含 FDE                              → FDE实践
    标题含 RAG / Agent / langChain / langGraph → AI技术
    其余                                    → 商业思考
"""
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 仓库根（容器内 /app）

from app.notes_admin import NOTES_DIR, _extract_title, _git_commit, _rebuild_manifest, get_settings, _git

RULES = [
    ("FDE实践", re.compile(r"FDE", re.I)),
    ("AI技术", re.compile(r"RAG|Agent|langChain|langGraph", re.I)),
]
DEFAULT_CAT = "商业思考"


def classify(title: str) -> str:
    for cat, pat in RULES:
        if pat.search(title):
            return cat
    return DEFAULT_CAT


def main() -> None:
    notes = [p for p in NOTES_DIR.glob("*.md") if not p.name.startswith("_")]
    if not notes:
        print("根目录没有待迁移的笔记，无事可做")
        return
    moved = []
    for p in notes:
        cat = classify(_extract_title(p.read_text(encoding="utf-8")))
        # 保险：旧式相对图片路径升格为站点绝对路径（与前端 fixRelativeUrls 同规则）——
        # 目录化后 md 不再在 notes/ 根下，相对 img/ 的旧写法会指错位置
        text = p.read_text(encoding="utf-8")
        fixed = text.replace("](img/", "](/notes/img/")
        if fixed != text:
            p.write_text(fixed, encoding="utf-8")
        target = NOTES_DIR / cat / p.name
        if target.exists():
            print(f"跳过（目标已存在）：{p.name} → {cat}/")
            continue
        (NOTES_DIR / cat).mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(target))
        moved.append(cat)
        print(f"移动：{p.name} → {cat}/{p.name}")
    if not moved:
        print("没有实际移动任何文件")
        return
    _rebuild_manifest()
    result = _git_commit("site/notes", f"{len(moved)} 篇存量笔记", "目录化迁移")
    print("git:", result)
    if get_settings().notes_git_push:
        _git(["pull", "--rebase", "--autostash", "origin", "main"])
        push = _git(["push", "origin", "HEAD"])
        print("push:", "成功" if push.returncode == 0 else push.stderr.strip()[:200])
    print(f"完成：{len(moved)} 篇已入目录")


if __name__ == "__main__":
    main()
