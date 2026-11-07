# Yubble 个人主页（独立项目）

个人门面：主页 / 作品 / 学习笔记（Git 即 CMS 写作台）。与零食店 AI 助手（snack-store-agent）是完全独立的两个项目：不同仓库、不同服务、不同口令。

## 结构

```
site/                    纯静态站（index / works / notes.html / style.css / notes/*.md）
app/                     极小 FastAPI：托管 site/ + 笔记发布/插图/清单 API
  ├── main.py            入口 + 轻限流（60 次/分，401×10 封 10 分钟）
  ├── notes_admin.py     发布 = 写 md + git commit；插图上传；index.json 清单
  └── config.py          读 .env（NOTES_TOKEN）
```

## 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # 填 NOTES_TOKEN
.venv/bin/uvicorn app.main:app --port 8001
# 打开 http://127.0.0.1:8001/notes.html
```

## 写笔记

打开 `/notes.html` → 写一篇 → 提交发布（输一次 NOTES_TOKEN，浏览器记住）。
发布 = md 写入 `site/notes/` + 自动生成 `index.json` 清单 + git commit。落盘即上线。

插图：编辑器工具栏直接上传，落 `site/notes/img/`，随下次发布一起提交。

## 部署

任意能跑 Python 的机器：clone → `pip install -r requirements.txt` → uvicorn 起服务，nginx 反代根路径。Agent（snack-store-agent）与本服务互不依赖，可各自独立升级/重启。
