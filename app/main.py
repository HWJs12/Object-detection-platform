"""FastAPI 应用入口: 路由注册 / 异常处理 / 静态资源挂载。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import detect, model, records, stats
from app.core.yolo_engine import engine
from app.db.database import close_connection, init_db
from app.utils.exceptions import AppError
from app.utils.response import ok
from config import BASE_DIR, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("underwater")

WEB_DIR = BASE_DIR / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动: 初始化数据库 + 后台预热模型; 关闭: 释放连接。"""
    try:
        init_db()
        logger.info("数据库初始化完成: %s", settings.db_path)
    except Exception as exc:
        logger.error("数据库初始化失败: %s", exc)
        raise

    model_path = settings.model_path_abs
    logger.info("模型路径: %s (存在: %s)", model_path, bool(model_path and model_path.exists()))
    logger.info("推理设备: %s", settings.device_resolved)

    # 后台预热模型: 不阻塞启动, 首次检测无需再等待加载
    if model_path and model_path.exists():
        async def _warmup() -> None:
            try:
                from fastapi.concurrency import run_in_threadpool
                await run_in_threadpool(engine.load)
                logger.info("模型预热完成: %s", model_path.name)
            except Exception as exc:  # 预热失败不影响服务启动
                logger.warning("模型预热失败: %s", exc)

        asyncio.create_task(_warmup())
    else:
        logger.warning("未检测到模型权重文件, 检测接口将返回明确错误")

    yield
    close_connection()


app = FastAPI(
    title="基于GSCNet的海洋检测平台",
    description="基于 GSCNet 的水下海洋生物检测 Web 系统: 图片检测 / 视频实时检测 / 历史记录 / 数据统计",
    version="1.0.0",
    lifespan=lifespan,
)

# 本地开发, 允许跨域(前端若用其它端口打开也不会被拦截)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 统一异常处理 ----------------


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """业务异常: 返回用户可读信息, 不崩溃。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "code": exc.code, "message": exc.message, "data": None},
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常: 记录日志并返回统一结构。"""
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "code": "INTERNAL_ERROR",
            "message": f"服务器内部错误: {exc}",
            "data": None,
        },
    )


# ---------------- 路由 ----------------


@app.get("/api/health", summary="健康检查", tags=["系统"])
async def health() -> Dict[str, Any]:
    return ok({
        "status": "running",
        "device": settings.device_resolved,
        "model_exists": bool(settings.model_path_abs and settings.model_path_abs.exists()),
        "model_path": str(settings.model_path_abs) if settings.model_path_abs else None,
    })


app.include_router(detect.ws_router)
app.include_router(detect.router)
app.include_router(records.router)
app.include_router(stats.router)
app.include_router(model.router)

# ---------------- 静态资源 ----------------
# /data -> 上传与结果文件; / -> 前端页面(必须最后挂载)
app.mount("/data", StaticFiles(directory=str(settings.data_dir), check_dir=False), name="data")
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True, check_dir=False), name="web")
