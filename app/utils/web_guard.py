"""HTTP 入口保护: 本机访问、同源校验和请求体大小上限。"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit


def _is_loopback(client: tuple | None) -> bool:
    if not client:
        return False
    try:
        address = ipaddress.ip_address(str(client[0]).split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _is_loopback_host(host_header: str) -> bool:
    """只接受 localhost 与 loopback IP Host, 阻断 DNS rebinding 域名。"""
    if not host_header:
        return False
    try:
        hostname = urlsplit(f"//{host_header}").hostname
        if not hostname:
            return False
        if hostname.lower() == "localhost":
            return True
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
        return address.is_loopback
    except ValueError:
        return False


class WebGuardMiddleware:
    """不带账号体系的本地工具只接受 loopback，并限制上传及普通请求体。"""

    def __init__(self, app, max_upload_bytes: int, max_batch_bytes: int,
                 max_body_bytes: int = 2 * 1024 * 1024) -> None:
        self.app = app
        self.max_upload_bytes = max_upload_bytes
        self.max_batch_bytes = max_batch_bytes
        self.max_body_bytes = max_body_bytes

    async def _reject(self, send, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {"success": False, "code": code, "message": message, "data": None},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if not _is_loopback(scope.get("client")):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 4403})
            else:
                await self._reject(send, 403, "LOCAL_ONLY", "此服务仅允许本机访问")
            return

        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        host = headers.get("host", "")
        if not _is_loopback_host(host):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 4403})
            else:
                await self._reject(send, 403, "INVALID_HOST", "仅允许通过本机地址访问")
            return

        path = scope.get("path", "")
        if path.startswith("/api/") or path.startswith("/ws/"):
            origin = headers.get("origin")
            host = host.lower()
            if origin:
                try:
                    parsed = urlsplit(origin)
                    same_host = parsed.scheme in ("http", "https") and parsed.netloc.lower() == host
                except ValueError:
                    same_host = False
                if not same_host:
                    if kind == "websocket":
                        await send({"type": "websocket.close", "code": 4403})
                    else:
                        await self._reject(send, 403, "CROSS_ORIGIN", "跨站请求已拒绝")
                    return

        if kind == "websocket":
            await self.app(scope, receive, send)
            return

        limit = self.max_body_bytes
        if path == "/api/detect/image" or path == "/api/detect/video":
            limit = self.max_upload_bytes + 1024 * 1024
        elif path == "/api/detect/images":
            limit = self.max_batch_bytes + 1024 * 1024

        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await self._reject(send, 413, "REQUEST_TOO_LARGE", "请求体超过服务端大小限制")
                    return
            except ValueError:
                await self._reject(send, 400, "INVALID_CONTENT_LENGTH", "Content-Length 无效")
                return

        received = 0
        exceeded = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            if not exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await self._reject(send, 413, "REQUEST_TOO_LARGE", "请求体超过服务端大小限制")
