"""FastAPI 应用入口: 路由注册 / 异常处理 / 静态资源挂载。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import detect, model, records, stats, system
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
    """启动: 初始化数据库 / 清理过期文件 / 回收僵尸任务 / 后台预热模型; 关闭: 释放连接。"""
    try:
        init_db()
        logger.info("数据库初始化完成: %s", settings.db_path)
    except Exception as exc:
        logger.error("数据库初始化失败: %s", exc)
        raise

    # 资源治理: 过期文件清理 + 僵尸任务回收 + 上传中断残留清理
    try:
        if settings.auto_cleanup:
            from fastapi.concurrency import run_in_threadpool

            from app.core.cleaner import cleanup_expired, purge_tmp_files

            stat = await run_in_threadpool(cleanup_expired)
            if not stat.get("skipped") and stat.get("records"):
                logger.info("启动清理: 删除 %d 条过期记录, 回收 %.1f MB",
                            stat["records"], stat["freed_bytes"] / 1024 / 1024)
            tmp = await run_in_threadpool(purge_tmp_files)
            if tmp:
                logger.info("启动清理: 删除 %d 个残留临时文件", tmp)
        from app.core.video_task import task_manager

        task_manager.reap()
    except Exception as exc:
        logger.warning("启动清理失败(不影响服务): %s", exc)

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
    # 关闭内置 /docs, 由下方 custom_swagger_ui 接管(追加中文说明)
    docs_url=None,
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
    """兜底异常: 详情只写日志, 对外返回通用文案。

    不把原始异常内容回显给用户 —— 那会暴露文件路径、SQL 语句、依赖内部信息。
    排查靠服务端日志(含完整堆栈)。
    """
    logger.exception("未处理异常 [%s %s]: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "code": "INTERNAL_ERROR",
            "message": "服务器内部错误, 请稍后重试; 若持续出现请查看服务端日志",
            "data": None,
        },
    )


# ---------------- 路由 ----------------


@app.get("/api/health", summary="健康检查", tags=["系统"])
async def health() -> Dict[str, Any]:
    """服务健康状态 + 资源占用概况。"""
    from app.core.cleaner import storage_stats
    from app.core.video_task import task_manager

    return ok({
        "status": "running",
        "device": settings.device_resolved,
        "model_exists": bool(settings.model_path_abs and settings.model_path_abs.exists()),
        "model_path": str(settings.model_path_abs) if settings.model_path_abs else None,
        "storage": storage_stats(),
        "video_tasks": task_manager.active_count(),
        "tracking_busy": _tracking_busy(),
    })


@app.post("/api/system/cleanup", summary="手动触发资源清理", tags=["系统"])
async def system_cleanup(
    days: int = Query(0, ge=0, le=3650, description="过期天数, 0 表示按配置值"),
    duplicates: bool = Query(True, description="是否一并清理内容重复的历史文件"),
    dry_run: bool = Query(False, description="只统计不删除, 用于预检"),
) -> Dict[str, Any]:
    """手动清理磁盘: 过期文件及其记录 + 重复副本 + 上传残留。

    有视频任务正在运行时拒绝执行, 避免删除任务正在使用的文件。
    """
    from fastapi.concurrency import run_in_threadpool

    from app.core.cleaner import cleanup_duplicates, cleanup_expired, purge_tmp_files
    from app.core.video_task import task_manager
    from app.utils.exceptions import AppError

    if len(task_manager) > 0:
        raise AppError(
            f"当前有 {len(task_manager)} 个视频任务在队列中, 请等待结束后再清理",
            code="CLEANUP_BUSY",
            status_code=409,
        )

    result = await run_in_threadpool(
        lambda: {
            "expired": cleanup_expired(days or None),
            "duplicates": cleanup_duplicates(dry_run=dry_run) if duplicates else {"count": 0},
        }
    )
    tmp_removed = 0 if dry_run else await run_in_threadpool(purge_tmp_files)

    freed = result["expired"].get("freed_bytes", 0) + result["duplicates"].get("freed_bytes", 0)
    return ok({
        **result,
        "tmp_removed": tmp_removed,
        "freed_bytes": freed,
        "freed_mb": round(freed / 1024 / 1024, 2),
        "dry_run": dry_run,
    }, f"清理完成{' (预检, 未实际删除)' if dry_run else ''}, 可回收 {freed / 1024 / 1024:.1f} MB")


# ---------------- 存储治理 ----------------

storage_router = APIRouter(prefix="/api/storage", tags=["存储治理"])


@storage_router.get("/audit", summary="存储审计明细")
async def storage_audit_api() -> Dict[str, Any]:
    """完整存储审计: 有效 / 孤儿 / 回收站占用, 一眼看清空间去哪了。"""
    from fastapi.concurrency import run_in_threadpool

    from app.core.cleaner import storage_detail

    return ok(await run_in_threadpool(storage_detail))


@storage_router.post("/orphans/cleanup", summary="回收孤儿文件")
async def clean_orphans(
    dry_run: bool = Query(True, description="默认只预检, 传 false 才真正回收"),
    min_age_sec: int = Query(60, ge=0, le=86400, description="跳过最近修改过的文件(秒)"),
) -> Dict[str, Any]:
    """回收无任何记录引用的文件。

    与保留期无关 —— 只要没记录引用即可回收。默认 dry_run 防误操作。
    """
    from fastapi.concurrency import run_in_threadpool

    from app.core.storage import cleanup_orphans

    result = await run_in_threadpool(
        lambda: cleanup_orphans(dry_run=dry_run, to_trash=True, min_age_sec=min_age_sec)
    )
    freed = result["freed_bytes"]
    return ok(
        {**result, "freed_mb": round(freed / 1024 / 1024, 2), "dry_run": dry_run},
        f"{'预检: 可' if dry_run else '已'}回收 {result['count']} 个孤儿文件, "
        f"{freed / 1024 / 1024:.2f} MB",
    )


@storage_router.get("/trash", summary="回收站概况")
async def trash_api() -> Dict[str, Any]:
    """回收站当前占用与最老文件滞留天数。"""
    from fastapi.concurrency import run_in_threadpool

    from app.core.storage import trash_stats

    return ok(await run_in_threadpool(trash_stats))


@storage_router.post("/trash/purge", summary="清空回收站")
async def purge_trash_api(
    days: int = Query(7, ge=0, le=365, description="仅清除滞留超过该天数的文件"),
) -> Dict[str, Any]:
    """彻底删除回收站中超过滞留窗口的文件, 真正释放磁盘。"""
    from fastapi.concurrency import run_in_threadpool

    from app.core.storage import purge_trash

    result = await run_in_threadpool(lambda: purge_trash(older_than_days=days))
    return ok(
        {**result, "freed_mb": round(result["freed_bytes"] / 1024 / 1024, 2)},
        f"回收站已清理 {result['count']} 个文件, 释放 {result['freed_bytes'] / 1024 / 1024:.2f} MB",
    )


@storage_router.post("/trash/restore", summary="从回收站恢复文件")
async def restore_trash_api(
    name: str = Query(..., description="回收站中的文件名"),
) -> Dict[str, Any]:
    """把误删的文件从回收站恢复到原目录。"""
    from fastapi.concurrency import run_in_threadpool

    from app.core.storage import restore_from_trash

    result = await run_in_threadpool(lambda: restore_from_trash(name))
    if not result:
        from app.utils.exceptions import AppError
        raise AppError("回收站中未找到该文件", code="TRASH_NOT_FOUND", status_code=404)
    return ok(result, f"已恢复: {result['restored']}")


app.include_router(storage_router)


def _tracking_busy() -> bool:
    try:
        from app.core.yolo_engine import engine
        return engine.track_busy()
    except Exception:
        return False


# ---------------- 接口文档页(中文标注) ----------------
# FastAPI 内置 /docs 由 swagger-ui 渲染, 默认 BaseLayout 不产出顶部栏,
# 因此 Swagger UI / ReDoc 原生按钮不会出现。这里自建一条顶部导航:
# 两个按钮 + 各自正下方的中文用途标注, 并提供用途说明区块。

_DOCS_ZH_HINT = """
<div id="zh-docs-bar" style="background:#1F2937">
  <div style="max-width:1100px;margin:0 auto;padding:14px 16px;
              display:flex;gap:32px;align-items:flex-start;
              font-family:system-ui,'Microsoft YaHei',sans-serif">
    <a href="/docs" style="display:flex;flex-direction:column;align-items:center;
                           text-decoration:none">
      <span style="background:#5B6B7F;color:#fff;font-size:15px;
                   padding:8px 20px;border-radius:6px;display:inline-block;
                   min-width:96px;text-align:center">Swagger UI</span>
      <span style="margin-top:6px;font-size:13px;color:#7FB2E5;
                   font-weight:500;white-space:nowrap">在线调试控制台</span>
    </a>
    <a href="/redoc" style="display:flex;flex-direction:column;align-items:center;
                            text-decoration:none">
      <span style="background:#5B6B7F;color:#fff;font-size:15px;
                   padding:8px 20px;border-radius:6px;display:inline-block;
                   min-width:96px;text-align:center">ReDoc</span>
      <span style="margin-top:6px;font-size:13px;color:#7FB2E5;
                   font-weight:500;white-space:nowrap">阅读版接口手册</span>
    </a>
  </div>
</div>
<div style="max-width:1100px;margin:12px auto 0;padding:0 16px;
            font-family:system-ui,'Microsoft YaHei',sans-serif;font-size:13px;
            color:#1E293B;line-height:1.9">
  <div style="background:#F1F5F9;border-left:4px solid #2E75B6;
              border-radius:6px;padding:10px 16px">
    <b>Swagger UI</b>（在线调试控制台）：列出全部接口，可展开填写参数并点击
    <b>Execute</b> 直接发起请求，实时查看返回结果，适合边调边试。<br>
    <b>ReDoc</b>（阅读版接口手册）：同一份接口文档的只读排版，左侧带目录树，
    适合完整通读或转发给他人查阅，不能发起请求。
  </div>
</div>
"""


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui(request: Request) -> HTMLResponse:
    """在 FastAPI 内置 Swagger UI 顶部追加中文导航与用途说明。"""
    root_path = request.scope.get("root_path", "").rstrip("/")
    html = get_swagger_ui_html(
        openapi_url=root_path + app.openapi_url,
        title=f"{app.title} - 接口调试",
        swagger_ui_parameters=app.swagger_ui_parameters,
    ).body.decode("utf-8")
    # 说明块插在 swagger-ui 容器之前(即页面最顶部)
    html = html.replace(
        '<div id="swagger-ui">', _DOCS_ZH_HINT + '<div id="swagger-ui">', 1
    )
    return HTMLResponse(html)


app.include_router(system.router)
app.include_router(detect.ws_router)
app.include_router(detect.router)
app.include_router(records.router)
app.include_router(records.report_router)
app.include_router(stats.router)
app.include_router(model.router)

# ---------------- 静态资源 ----------------
# /data -> 上传与结果文件; / -> 前端页面(必须最后挂载)


class _NoCacheStaticFiles(StaticFiles):
    """前端源码类资源禁用强缓存, 避免改了 HTML/JS/CSS 但浏览器仍用旧副本。

    只对有"代码语义"的扩展名加 no-cache(仍允许协商缓存走 ETag/304, 不浪费带宽);
    图片 / 视频等大文件保持默认缓存策略, 不影响加载性能。
    """

    _NO_CACHE_EXT = {".html", ".htm", ".js", ".css", ".json", ".svg", ".ico"}

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        ext = os.path.splitext(path)[1].lower()
        if ext in self._NO_CACHE_EXT:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# 只暴露受管的上传 / 结果目录, 不挂载整个 data 目录 ——
# 否则 .trash/(已删除文件) 与 cleanup.log(审计日志) 会被直接下载到。
# 前端引用的 URL 形态 /data/uploads/xxx 与 /data/results/xxx 保持不变。
app.mount("/data/uploads", StaticFiles(directory=str(settings.upload_dir), check_dir=False),
          name="uploads")
app.mount("/data/results", StaticFiles(directory=str(settings.result_dir), check_dir=False),
          name="results")
app.mount("/", _NoCacheStaticFiles(directory=str(WEB_DIR), html=True, check_dir=False), name="web")
