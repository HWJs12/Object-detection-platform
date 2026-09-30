"""项目启动入口。

用法:
    python run.py                    # 默认 127.0.0.1:8000
    python run.py --port 8080        # 指定端口
    python run.py --host 0.0.0.0     # 允许局域网访问
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BASE_DIR, settings  # noqa: E402


def is_port_free(host: str, port: int) -> bool:
    """判断端口是否空闲。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) != 0


def pick_port(host: str, port: int) -> int:
    """端口被占用时自动向后寻找可用端口。"""
    for candidate in range(port, port + 20):
        if is_port_free(host, candidate):
            return candidate
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description="水下海洋生物目标检测与分析系统")
    parser.add_argument("--host", default=settings.host, help="监听地址, 默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=settings.port, help="监听端口, 默认 8000")
    parser.add_argument("--reload", action="store_true", help="开启热重载(开发用)")
    args = parser.parse_args()

    port = pick_port(args.host, args.port)
    if port != args.port:
        print(f"[提示] 端口 {args.port} 已被占用, 自动切换到 {port}")
        print(f"       如果占用者是之前未关闭的本系统实例, 先关闭它可继续使用 {args.port}:")
        print(f"         netstat -ano | findstr :{args.port}")
        print(f"         taskkill /PID <上面最后一列的PID> /F")

    model_path = settings.model_path_abs
    print("=" * 62)
    print("  水下海洋生物目标检测与分析系统")
    print("=" * 62)
    print(f"  访问地址: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{port}")
    print(f"  模型文件: {model_path if model_path else '未配置(请将 .pt 放入 models/ 目录)'}")
    print(f"  推理设备: {settings.device_resolved}")
    print(f"  数据库  : {settings.db_path}")
    print(f"  项目目录: {BASE_DIR}")
    if not model_path or not model_path.exists():
        print("  [警告] 未检测到模型权重文件, 检测功能将不可用")
    print("=" * 62)

    # 依赖缺失时给出明确的解决指引, 而不是抛出看不懂的 traceback
    try:
        import uvicorn
    except ModuleNotFoundError:
        VENV_PY = r"C:\Users\12503\.workbuddy\binaries\python\envs\underwater\Scripts\python.exe"
        print("\n[错误] 当前 Python 环境缺少依赖(uvicorn), 说明没有使用项目虚拟环境!")
        print("解决方法(任选其一):")
        print("  1. 双击项目根目录的 start.bat 启动(自动使用正确的环境)")
        print(f"  2. 在 Trae/VSCode 中按 Ctrl+Shift+P -> 'Python: 选择解释器'")
        print(f"     -> 输入并选择: {VENV_PY}")
        print(f"  3. 直接在终端运行: \"{VENV_PY}\" run.py")
        print("  4. 或先安装依赖: pip install -r requirements.txt")
        sys.exit(1)

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=args.reload or settings.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
