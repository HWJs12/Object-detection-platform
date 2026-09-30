"""统一 API 响应结构。

所有接口均返回:
    { "success": bool, "code": "OK"/"...", "message": "...", "data": {...} }
"""

from typing import Any, Dict, Optional


def ok(data: Any = None, message: str = "操作成功") -> Dict[str, Any]:
    return {"success": True, "code": "OK", "message": message, "data": data}


def fail(message: str, code: str = "ERROR", data: Optional[Any] = None) -> Dict[str, Any]:
    return {"success": False, "code": code, "message": message, "data": data}
