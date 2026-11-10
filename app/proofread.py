"""AI 校对：点提交发布后、真正落库前，用大模型过一遍错别字/拼写/大小写。

走 OpenAI 兼容接口，urllib 标准库直调——个人站不加任何 SDK 依赖。
与零食店 Agent 共用同一把 key，但服务、配置、仓库完全独立。
口令保护：AI 调用是真金白银，匿名请求一律 401。
"""
import json
import re
import urllib.request
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.notes_admin import _require_notes_token

router = APIRouter(prefix="/api/admin/notes", tags=["notes-proofread"])

MAX_PROOFREAD_CHARS = 8000   # 超长截断送检，控制 token 成本
LLM_TIMEOUT = 60             # 秒：长文检查给足时间
# 2026-11-10 线上案例：3 个问题就花 1222 completion tokens，2000 上限下
# 报 6 个问题即截断（finish_reason=length），JSON 断在半截 →「AI 返回格式异常」。
# 提到 4000（issues 上限 20 个约 2600 tokens，留足余量）。
MAX_OUTPUT_TOKENS = 4000


class ProofIn(BaseModel):
    content: str


PROOF_SYSTEM = """你是中文博客文章的校对编辑。检查文章并只报告有把握的问题：
1. 错别字/用词错误（上下文能确认的，如"在提交"应为"再提交"）
2. 英文拼写错误（技术名词以官方拼写为准，如 LangChain、LangGraph、ChatGPT、RAG）
3. 大小写不一致（同一名词在文中出现两种写法，如 langChain 与 LangChain 混用）
不要报：中英文之间的空格、风格偏好、标点全半角、纯语法建议。
拿不准的问题不要报，宁缺毋滥。严格输出 JSON（不要多余文字）：
{"issues": [{"type": "错别字|拼写|大小写", "original": "原文片段", "suggest": "建议改为", "reason": "简短理由"}], "summary": "一句话总评"}"""


def _chat(system: str, user: str) -> tuple[str, str]:
    """调用校对模型，返回 (内容, finish_reason)。失败路径要靠 finish_reason 定位截断。"""
    s = get_settings()
    if not s.llm_api_key or s.llm_api_key == "sk-placeholder":
        raise RuntimeError("未配置 LLM_API_KEY")
    body = json.dumps({
        "model": s.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        s.llm_base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + s.llm_api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    choice = data["choices"][0]
    return choice["message"].get("content") or "", choice.get("finish_reason", "")


@router.post("/proofread")
def proofread(
    body: ProofIn,
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """校对笔记内容，返回问题清单。失败不拦发布——前端降级直发。"""
    _require_notes_token(x_notes_token or x_admin_token)
    text = body.content[:MAX_PROOFREAD_CHARS]
    if len(text.strip()) < 10:
        return {"issues": [], "summary": "内容太短，无需检查", "checked_chars": len(text.strip())}
    try:
        raw, finish = _chat(PROOF_SYSTEM, text)
    except Exception as e:   # 网络失败/格式异常统一 502，前端据此降级
        print(f"[proofread] 模型调用失败：{type(e).__name__}: {e}")
        raise HTTPException(502, f"AI 服务不可用：{e}")
    m = re.search(r"\{[\s\S]*\}", raw)
    try:
        data = json.loads(m.group(0) if m else raw)
        issues = data.get("issues", [])
    except (json.JSONDecodeError, AttributeError):
        # 失败细节必须落日志（docker logs 可查），否则只有前端一句"格式异常"没法排障
        print(f"[proofread] JSON 解析失败：finish_reason={finish}，"
              f"内容长度={len(raw)}，开头={raw[:150]!r}")
        if finish == "length":
            raise HTTPException(502, "AI 输出超长被截断（请稍后重试）")
        raise HTTPException(502, f"AI 返回格式异常（finish_reason={finish}）")
    return {
        "issues": issues[:20],
        "summary": data.get("summary", ""),
        "checked_chars": len(text),
    }
