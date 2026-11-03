"""AI 助手接口: 开关状态查询 + 流式对话。

安全边界:
    密钥只存在于服务端配置中, 前端永远拿不到, 也不直接访问上游 API;
    所有请求都经本模块转发。

降级边界:
    助手未启用时直接拒绝(503), 不发起任何外部请求; 上游异常时由 llm 模块
    在流内降级为提示文案, 不影响本服务的其它接口。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse

from app.core import llm
from app.utils.exceptions import AppError
from app.utils.response import ok
from config import settings

router = APIRouter(prefix="/api/chat", tags=["AI 助手"])

# 关闭各级代理与浏览器的缓冲, 保证增量内容实时到达
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/status", summary="AI 助手开关状态")
async def chat_status() -> Dict[str, Any]:
    """返回助手是否可用。

    前端据此决定是否渲染助手入口 —— 未启用时页面上不会出现任何助手元素,
    因此不会产生请求, 也不会产生费用。
    """
    ready = llm.is_ready()
    return ok({
        "enabled": ready,
        "model": settings.llm_model if ready else None,
    })


@router.post("/stream", summary="AI 助手流式对话")
async def chat_stream(payload: Dict[str, Any] = Body(default={})) -> StreamingResponse:
    """以 SSE 流式返回助手回复, 逐字推送至前端。

    请求体:
        messages: [{"role": "user"|"assistant", "content": "..."}]
    """
    if not llm.is_ready():
        raise AppError(
            "AI 助手未启用。请将 config.yaml 中的 llm_enabled 设为 true, "
            "并通过环境变量 LLM_API_KEY 提供密钥。",
            code="LLM_DISABLED",
            status_code=503,
        )

    messages: List[Dict[str, Any]] = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise AppError("messages 不能为空", code="INVALID_MESSAGES")

    return StreamingResponse(
        llm.stream_chat(messages),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
