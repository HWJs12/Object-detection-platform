"""DeepSeek 对话客户端: 为接口文档页提供 AI 答疑。

非阻塞设计(核心):
    全程使用 httpx.AsyncClient 异步流式, 不占用事件循环线程, 也不依赖线程池。
    同步 HTTP 客户端在流式场景下无法逐块返回, 若强行用 "线程 + 队列" 桥接,
    既增加复杂度又容易引入新的并发缺陷, 故不采用。

容错设计:
    任何异常(网络不可达 / 超时 / 鉴权失败 / 上游限流)都不向上抛出, 而是在
    流内降级为一句可读提示, 由前端正常显示后结束 —— 保证助手故障绝不会
    影响检测、统计等核心功能。

知识来源:
    系统提示词注入由 OpenAPI 描述压缩而成的接口清单, 使回答有据可依,
    不会编造不存在的接口或参数。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# 单次会话最多带上的历史消息条数(防止把整本书塞进来)
MAX_HISTORY_MESSAGES = 20
# 单条消息最大字符数
MAX_MESSAGE_CHARS = 4000
# 注入提示词的接口清单字符上限(防止极端情况下提示词无限膨胀)
MAX_CATALOG_CHARS = 24000

_client: Optional[httpx.AsyncClient] = None
_semaphore: Optional[asyncio.Semaphore] = None
_catalog_text: Optional[str] = None
_catalog_count: int = 0


# ------------------------------------------------------------------ 生命周期


def is_ready() -> bool:
    """助手是否可用(已启用且能取到密钥)。"""
    return settings.llm_ready


def _get_client() -> httpx.AsyncClient:
    """惰性创建并复用异步客户端(复用连接, 避免每次请求重建 TCP)。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.llm_connect_timeout,
                read=settings.llm_read_timeout,
                write=30.0,
                pool=settings.llm_connect_timeout,
            ),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=False,
        )
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    """并发闸门: 限制同时进行的对话数, 避免用户狂点拖垮上游与本地。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, int(settings.llm_max_concurrency)))
    return _semaphore


async def aclose() -> None:
    """关闭客户端(进程退出时调用)。"""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


# ------------------------------------------------------------------ 接口清单


def _iter_operations() -> List[Dict[str, Any]]:
    """从 OpenAPI 描述中提取全部接口的紧凑信息。"""
    try:
        # 局部导入: 避免与 app.main 形成循环依赖
        from app.main import app
        spec = app.openapi()
    except Exception as exc:
        logger.warning("无法生成 OpenAPI 描述, 助手将缺少接口清单: %s", exc)
        return []

    schemas = (spec.get("components") or {}).get("schemas") or {}
    ops: List[Dict[str, Any]] = []

    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue

            parts = [f"{method.upper()} {path}"]
            summary = (op.get("summary") or "").strip()
            if summary:
                parts.append(f"— {summary}")

            params = []
            for p in op.get("parameters") or []:
                name = p.get("name") or ""
                if not name:
                    continue
                mark = "必填" if p.get("required") else "可选"
                desc = (p.get("description") or "").strip()
                params.append(f"{name}({mark}{'，' + desc if desc else ''})")
            if params:
                parts.append("| 参数: " + "、".join(params))

            body_fields = _body_fields(op, schemas)
            if body_fields:
                parts.append("| 请求体: " + "、".join(body_fields))

            ops.append({"line": " ".join(parts)})

    return ops


def _body_fields(op: Dict[str, Any], schemas: Dict[str, Any]) -> List[str]:
    """提取请求体的字段清单(仅取第一个 content 类型)。"""
    rb = op.get("requestBody")
    if not rb:
        return []
    for _ctype, c in (rb.get("content") or {}).items():
        schema = c.get("schema") or {}
        ref = schema.get("$ref")
        if not ref:
            return []
        model = schemas.get(ref.rsplit("/", 1)[-1]) or {}
        required = set(model.get("required") or [])
        out = []
        for name, spec in (model.get("properties") or {}).items():
            mark = "必填" if name in required else "可选"
            desc = (spec.get("description") or "").strip()
            out.append(f"{name}({mark}{'，' + desc if desc else ''})")
            if len(out) >= 12:
                break
        return out
    return []


def _ensure_catalog() -> None:
    """构建并缓存接口清单(首次调用后不再重复生成 OpenAPI)。"""
    global _catalog_text, _catalog_count
    if _catalog_text is not None:
        return
    ops = _iter_operations()
    _catalog_count = len(ops)
    text = "\n".join(o["line"] for o in ops)
    if len(text) > MAX_CATALOG_CHARS:
        text = text[:MAX_CATALOG_CHARS] + "\n...(其余接口已省略)"
    _catalog_text = text or "(未能读取到接口清单)"


_SYSTEM_HEAD = """你是「基于 GSCNet 的水下海洋生物检测平台」的接口答疑助手，服务于本平台的接口文档页。

## 你的职责
1. 解答平台接口的用法：参数含义、是否必填、取值范围、返回值结构、错误处理
2. 根据用户描述的任务，给出可直接运行的调用示例（cURL 或 Python requests）
3. 解释平台的整体功能与典型使用流程

## 平台基本信息
- 统一响应体：{"success": true/false, "code": "...", "message": "...", "data": {...}}
  失败时 success 为 false，原因在 message，机器可读错误码在 code
- 检测类别：海胆 / 海星 / 海参 / 扇贝 / 海草
- 分页参数：page（从 1 开始）、page_size
- 本页提供 Swagger UI 与 ReDoc 两种交互式调试入口

## 接口清单（共 {count} 个。请严格据此回答，不要编造不存在的接口或参数）
{catalog}

## 回答要求
- 用中文，简洁直接，先给结论再给示例
- 涉及接口时必须给出准确的路径与方法，参数名与清单保持一致
- 若问题与平台或接口无关，也请正常回答，但先说明这超出接口答疑范围
- 不确定的内容直接说明不确定，不要编造
"""


def build_system_prompt() -> str:
    """构造系统提示词(含全部接口清单)。"""
    _ensure_catalog()
    return (
        _SYSTEM_HEAD.replace("{count}", str(_catalog_count))
        .replace("{catalog}", _catalog_text or "")
    )


# ------------------------------------------------------------------ 流式对话


def _sse(obj: Dict[str, Any]) -> str:
    """打包为一条 SSE 消息。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _sanitize(messages: Any) -> List[Dict[str, str]]:
    """裁剪并规范化历史消息: 限条数、限长度、去非法角色、末尾必须是用户发言。"""
    if not isinstance(messages, list):
        return []
    out: List[Dict[str, str]] = []
    for m in messages[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        content = str(m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    while out and out[-1]["role"] != "user":   # 必须以用户提问收尾
        out.pop()
    return out


def _extract_delta(data: str) -> str:
    """从一条 SSE data 中取出增量文本。"""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


async def _read_error(resp: httpx.Response) -> str:
    try:
        return (await resp.aread()).decode("utf-8", "replace")
    except Exception:
        return ""


def _friendly_http_error(status: int) -> str:
    """把上游状态码翻译成用户能看懂的提示。"""
    if status == 401:
        return "助手密钥无效或已过期，请检查配置。"
    if status == 402:
        return "助手账户余额不足，请先充值。"
    if status == 429:
        return "提问过于频繁，请稍后再试。"
    if status >= 500:
        return "助手服务暂时不可用，请稍后重试。"
    return f"助手请求失败（HTTP {status}）。"


async def stream_chat(messages: Any) -> AsyncIterator[str]:
    """流式对话: 逐块 yield SSE 字符串。

    本生成器保证正常结束(不向外抛异常), 使 StreamingResponse 能把已产出的
    内容完整送达前端; 失败时以一条 error 事件收尾, 由前端展示提示。
    """
    if not is_ready():
        yield _sse({"type": "error", "message": "AI 助手未启用。"})
        return

    cleaned = _sanitize(messages)
    if not cleaned:
        yield _sse({"type": "error", "message": "消息内容为空。"})
        return

    base_payload = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": build_system_prompt()}] + cleaned,
        "stream": True,
        "max_tokens": int(settings.llm_max_tokens),
        "temperature": 0.3,
    }
    # 请求变体: 优先尝试关闭思考模式(接口答疑不需要推理链, 可显著降低首字延迟
    # 与输出开销); 若上游不接受该参数, 会自动去掉重试一次(见下方 400 分支)。
    variants: List[Dict[str, Any]] = []
    if settings.llm_disable_thinking:
        variants.append({**base_payload, "thinking": {"type": "disabled"}})
    variants.append(base_payload)

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key_effective}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"

    sem = _get_semaphore()
    acquired = False
    try:
        # 并发闸门: 短暂等待仍拿不到名额则直接告知, 不无限排队
        try:
            await asyncio.wait_for(sem.acquire(), timeout=10)
            acquired = True
        except asyncio.TimeoutError:
            yield _sse({"type": "error", "message": "当前提问较多，请稍后再试。"})
            return

        for idx, body in enumerate(variants):
            async with _get_client().stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code == 400 and idx + 1 < len(variants):
                    detail = await _read_error(resp)
                    logger.info("上游未接受思考开关参数, 去掉后重试: %s", detail[:120])
                    continue                      # 尚未产出任何内容, 可安全重试
                if resp.status_code != 200:
                    detail = await _read_error(resp)
                    logger.warning("DeepSeek 返回 %s: %s", resp.status_code, detail[:200])
                    yield _sse({"type": "error", "message": _friendly_http_error(resp.status_code)})
                    return
                async for line in resp.aiter_lines():
                    if not line or line.startswith(":"):     # 空行 / SSE keep-alive
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    piece = _extract_delta(chunk)
                    if piece:
                        yield _sse({"type": "delta", "text": piece})
                break                                  # 该变体成功, 不再尝试后续变体
        else:
            # 全部变体均被上游拒绝(仅在关闭思考的参数不被接受且无兜底变体时)
            yield _sse({"type": "error", "message": "助手请求参数被上游拒绝。"})
            return

        yield _sse({"type": "done"})

    except httpx.TimeoutException:
        logger.warning("DeepSeek 请求超时")
        yield _sse({"type": "error", "message": "助手响应超时，请稍后重试。"})
    except httpx.HTTPError as exc:
        logger.warning("DeepSeek 网络异常: %s", exc)
        yield _sse({"type": "error", "message": "无法连接助手服务，请检查网络。"})
    except asyncio.CancelledError:
        raise                                  # 客户端主动断开: 正常传播
    except Exception as exc:                   # 兜底: 绝不把异常抛给上层
        logger.exception("助手未预期异常: %s", exc)
        yield _sse({"type": "error", "message": "助手暂时不可用。"})
    finally:
        if acquired:
            try:
                sem.release()
            except Exception:
                pass
