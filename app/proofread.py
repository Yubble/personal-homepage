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
拿不准的问题不要报，宁缺毋滥。

输出规模硬约束（超长会导致 JSON 被截断、整次校对报废，务必遵守）：
- 最多报告 8 处问题：超过 8 处时只挑最有把握的，宁少勿多
- original 只写问题所在的词组（≤15 字），严禁复制整句或整段原文
- reason ≤20 字，一句话说清依据
- summary ≤40 字
严格输出 JSON（不要多余文字）：
{"issues": [{"type": "错别字|拼写|大小写", "original": "词组", "suggest": "建议改为", "reason": "简短理由"}], "summary": "一句话总评"}"""


def _chat(system: str, user: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> tuple[str, str, dict]:
    """调用校对模型，返回 (内容, finish_reason, usage)。失败路径要靠 finish_reason 定位截断。"""
    import time

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
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        # 校对是确定性文本任务，思考链纯浪费。2026-11-14 实测：1500 字送检，
        # 基线模型思考 3092 字、正文 0 字被 length 截断（推理模型对长输入会
        # 失控长考）；reasoning_effort=none 后正文 1255 字、思考 0、正常收尾。
        # 该参数本网关实测有效；其他网关不认识时报 400 → 走批次降级。
        "reasoning_effort": "none",
    }).encode("utf-8")
    req = urllib.request.Request(
        s.llm_base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + s.llm_api_key,
            "Content-Type": "application/json",
        },
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    elapsed = time.monotonic() - t0
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    # 每次调用都落监控日志：吞吐（tokens）、耗时、上下文规模——排障与成本一目了然
    print(f"[proofread] 用量: 输入={usage.get('prompt_tokens', '?')}tok "
          f"输出={usage.get('completion_tokens', '?')}tok "
          f"耗时={elapsed:.1f}s "
          f"送检字符={len(user)} "
          f"finish={choice.get('finish_reason', '?')}")
    _langfuse_track(choice["message"].get("content") or "", usage, elapsed, len(user))
    return choice["message"].get("content") or "", choice.get("finish_reason", ""), usage


def _langfuse_track(output: str, usage: dict, elapsed: float, chars: int) -> None:
    """上报本次校对到 Langfuse（可选能力：未配置/失败静默，不阻塞发布）。

    langfuse 5.x（v4 数据模型，2026-11-16 起 Cloud 仅支持 v4）：
    root span 承载整次校对，usage_details 记真实 tokens；
    metadata 值必须是 str；fastapi 噪音用 should_export_span 过滤。
    """
    try:
        from app.config import get_settings
        s = get_settings()
        if not (s.langfuse_public_key and s.langfuse_secret_key):
            return
        from langfuse import Langfuse
        from langfuse.span_filter import is_default_export_span
        from langfuse.types import TraceContext

        def _filter(span) -> bool:
            try:
                if not is_default_export_span(span):
                    return False
                scope = getattr(span, "instrumentation_scope", None)
                return scope is None or scope.name != "fastapi"
            except Exception:
                return True

        lf = Langfuse(public_key=s.langfuse_public_key,
                      secret_key=s.langfuse_secret_key,
                      host=s.langfuse_host,
                      should_export_span=_filter)
        span = lf.start_observation(
            name="note-proofread",
            metadata={"checked_chars": str(chars), "elapsed_sec": str(round(elapsed, 1))},
            trace_context=TraceContext(trace_id=lf.create_trace_id()),
        )
        span.update(
            output=output[:2000],
            model=s.llm_model,
            usage_details={
                "input": usage.get("prompt_tokens") or 0,
                "output": usage.get("completion_tokens") or 0,
            },
        )
        span.end()
        lf.flush()   # 短生命周期调用，立即落盘防丢
        lf.shutdown()
    except Exception as e:
        print(f"[proofread] Langfuse 上报失败（不影响业务）：{type(e).__name__}: {e}")


@router.post("/proofread")
def proofread(
    body: ProofIn,
    x_notes_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """校对笔记内容，返回问题清单。失败不拦发布——前端降级直发。

    分批策略（2026-11-14）：长文一次送检会触发推理模型失控长考（思考链吃光
    输出上限、正文 0 字被截断），改为按段落边界切 ~2000 字/批并发校对——
    每批输入短思考短、输出小不截断，总耗时 ≈ 最慢一批（并发 4 路）。
    单批失败只丢该批（结果标注可能不全），不拖垮整次校对。
    """
    _require_notes_token(x_notes_token or x_admin_token)
    text = body.content[:MAX_PROOFREAD_CHARS]
    if len(text.strip()) < 10:
        return {"issues": [], "summary": "内容太短，无需检查", "checked_chars": len(text.strip())}

    batches = _split_batches(text)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(4, len(batches))) as ex:
        results = list(ex.map(_check_batch, batches))

    ok = [r for r in results if r is not None]
    if not ok:
        raise HTTPException(502, f"全部 {len(batches)} 批校对失败，请稍后重试")
    skipped = len(batches) - len(ok)

    issues = [i for r in ok for i in r[0]][:20]
    summaries = [r[1] for r in ok if r[1]]
    summary = "；".join(summaries)[:120]
    if skipped:
        summary = f"（{skipped}/{len(batches)} 批未能检查，结果可能不全）{summary}"
    return {
        "issues": issues,
        "summary": summary,
        "checked_chars": len(text),
    }


BATCH_SIZE = 2000          # 每批送检字符数：输入短→思考短→输出不截断
BATCH_MAX_TOKENS = 2000    # 单批输出限额（禁思考后 2000 对 8 条约束绰绰有余）


def _split_batches(text: str, size: int = BATCH_SIZE) -> list[str]:
    """按行边界切批：凑满即切，单行超长再硬切兜底。"""
    batches: list[str] = []
    buf = ""
    for p in text.split("\n"):
        while len(p) > size:                      # 单行超长：硬切保底
            batches.append((buf + "\n" if buf else "") + p[:size])
            buf = ""
            p = p[size:]
        if len(buf) + len(p) + 1 <= size:
            buf += ("\n" if buf else "") + p
        else:
            if buf:
                batches.append(buf)
            buf = p
    if buf:
        batches.append(buf)
    return batches or [text]


def _check_batch(text: str) -> tuple[list, str] | None:
    """单批校对：返回 (issues, summary)；该批失败返回 None（降级跳过不拖垮整次）。"""
    try:
        raw, finish, _usage = _chat(PROOF_SYSTEM, text, max_tokens=BATCH_MAX_TOKENS)
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0) if m else raw)
        return data.get("issues", []), data.get("summary", "")
    except Exception as e:
        # 失败细节必须落日志，否则只有前端一句"批次失败"没法排障
        print(f"[proofread] 批次失败已跳过（{len(text)} 字）：{type(e).__name__}: {e}")
        return None
