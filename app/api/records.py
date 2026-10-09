"""历史记录接口: 查询 / 详情 / 删除 / 检测报告导出。"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.core import exporter
from app.core.storage import (
    append_audit,
    checkpoint_db,
    release_record_files,
)
from app.db import dao
from app.utils.exceptions import AppError, RecordNotFoundError
from app.utils.response import ok
from config import settings

router = APIRouter(prefix="/api/records", tags=["历史记录"])
# 报告导出不挂在 /records 前缀下(它是一类独立的输出能力)
report_router = APIRouter(prefix="/api/report", tags=["检测报告"])


@router.get("", summary="分页查询检测记录")
async def list_records(
    task_type: str = Query("", description="image | video, 留空为全部"),
    keyword: str = Query("", description="文件名关键字"),
    date_from: str = Query("", description="起始日期 YYYY-MM-DD"),
    date_to: str = Query("", description="结束日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
) -> Dict[str, Any]:
    rows, total = dao.list_records(
        task_type=task_type, keyword=keyword, date_from=date_from,
        date_to=date_to, page=page, page_size=page_size,
    )
    # 图片记录补充结果图访问地址; 视频记录补充结果视频地址
    for row in rows:
        if row.get("result_image"):
            row["result_url"] = f"/data/results/{row['result_image']}"
        if row.get("result_video"):
            row["result_video_url"] = f"/data/results/{row['result_video']}"
    return ok({
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    })


@router.get("/{record_id}", summary="查询单条记录详情")
async def get_record(record_id: int) -> Dict[str, Any]:
    record = dao.get_record(record_id)
    if not record:
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    if record.get("result_image"):
        record["result_url"] = f"/data/results/{record['result_image']}"
    if record.get("result_video"):
        record["result_video_url"] = f"/data/results/{record['result_video']}"

    record["items"] = dao.get_record_items(record_id)
    record["class_stats"] = dao.get_record_class_stats(record_id)
    record["frame_stats"] = dao.get_record_frame_stats(record_id)
    return ok(record)


@router.get("/{record_id}/ecology", summary="单条记录的生态多样性指标")
async def record_ecology(record_id: int) -> Dict[str, Any]:
    """按该条记录的类别计数计算生态指数, 供记录详情页展示。

    与全库生态指标用同一套公式(见 `app.core.ecology`), 保证口径一致。
    """
    from app.core.ecology import build_payload

    record = dao.get_record(record_id)
    if not record:
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    stats = dao.get_record_class_stats(record_id)
    # dao 返回 count 字段, ecology 直接消费该字段
    payload = await run_in_threadpool(build_payload, stats)
    payload["record_id"] = record_id
    payload["source_name"] = record.get("source_name")
    payload["task_type"] = record.get("task_type")
    return ok(payload)


@router.delete("/{record_id}", summary="删除检测记录")
async def delete_record(record_id: int) -> Dict[str, Any]:
    """删除记录并**同步释放其占用的磁盘文件**。

    去重机制下同一文件可能被多条记录共享, 因此删除前先取出记录快照,
    删行后再按引用计数判断文件是否真的无人使用 —— 仅当无引用时才回收。
    文件默认移入回收站(可恢复), 由保留窗口到期后彻底清除。
    """
    # 先快照: 删行后这些字段就查不到了
    record = dao.get_record(record_id)
    if not record:
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    if not dao.delete_record(record_id):
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    # 数据库事务已提交, 此时引用计数反映"剩余引用方", 可安全判定
    release = release_record_files(record, to_trash=True)
    if release["moved"]:
        checkpoint_db()

    return ok({
        "id": record_id,
        "files_freed": len(release["moved"]),
        "freed_bytes": release["freed_bytes"],
        "files_shared": release["kept"],   # 仍被其他记录引用, 保留
    }, "记录已删除, 关联文件已移入回收站")


# ---------------------------------------------------------------- 检测报告

_MAX_REPORT_RECORDS = 50  # 单份报告最多含多少张图, 防止超大请求


def _assemble_report_items(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把数据库记录拼装为报告所需的数据结构(含原图 URL 与检测明细)。"""
    items: List[Dict[str, Any]] = []
    for rec in records:
        item: Dict[str, Any] = {
            "source_name": rec.get("source_name"),
            "saved_name": rec.get("saved_name"),
            "result_image": rec.get("result_image"),
            "original_url": f"/data/uploads/{rec['saved_name']}" if rec.get("saved_name") else "",
            "total": int(rec.get("total_objects") or 0),
            "width": rec.get("image_width"),
            "height": rec.get("image_height"),
            "inference_ms": rec.get("inference_ms"),
            "conf_threshold": rec.get("conf_threshold"),
            "created_at": rec.get("created_at"),
        }
        # 明细: 数据库按 frame_index/confidence 排序, 图片记录的 frame_index 恒为 0
        dets = dao.get_record_items(rec["id"])
        item["detections"] = [{
            "class_id": d.get("class_id"),
            "class_name": d.get("class_name"),
            "confidence": d.get("confidence"),
            "bbox": [d.get("x1"), d.get("y1"), d.get("x2"), d.get("y2")],
        } for d in dets]
        stats = dao.get_record_class_stats(rec["id"])
        item["counts"] = [{"class_name": s["class_name"], "count": s["count"]} for s in stats]
        item["error"] = None if rec.get("status") != "failed" else (rec.get("message") or "检测失败")
        items.append(item)
    return items


def _attachment_response(content: bytes, filename: str, media_type: str,
                         extra: Optional[Dict[str, str]] = None) -> Response:
    """构造附件下载响应。中文文件名走 RFC 5987, 否则部分浏览器下载后乱码。"""
    quoted = urllib.parse.quote(filename)
    headers: Dict[str, str] = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}",
        "Content-Length": str(len(content)),
    }
    if extra:
        headers.update(extra)
    return Response(content=content, media_type=media_type, headers=headers)


def _load_records(ids: List[Any], action: str = "生成报告") -> List[Dict[str, Any]]:
    """按传入顺序取记录并校验存在性, 供 PDF 报告与标签导出共用。"""
    records: List[Dict[str, Any]] = []
    for rid in ids:
        try:
            rec = dao.get_record(int(rid))
        except (TypeError, ValueError):
            raise AppError(f"非法的记录 ID: {rid}", code="INVALID_RECORD_ID")
        if rec:
            records.append(rec)
    if not records:
        raise AppError(f"所选记录均已不存在, 无法{action}", code="RECORDS_NOT_FOUND")
    return records


@report_router.post("/images", summary="生成图片检测报告 PDF (按需生成)")
async def export_image_report(
    payload: Dict[str, Any] = Body(..., description='{"record_ids": [1,2,3]}'),
) -> Response:
    """按批次生成检测报告 PDF, 仅在调用时才生成并直接流式返回(不落盘)。

    请求体:
        {"record_ids": [101, 102, ...]}   本次上传的图片检测记录 ID 列表, 顺序即报告顺序
    """
    ids = (payload or {}).get("record_ids") or []
    if not isinstance(ids, list) or not ids:
        raise AppError("请提供要导出的记录 ID 列表", code="EMPTY_RECORDS")
    if len(ids) > _MAX_REPORT_RECORDS:
        raise AppError(
            f"单份报告最多包含 {_MAX_REPORT_RECORDS} 张影像, 当前 {len(ids)} 张",
            code="TOO_MANY_RECORDS",
        )

    # 按传入顺序取记录(保持与页面上一致的顺序), 并校验确实存在
    records = _load_records(ids, "生成报告")

    items = await run_in_threadpool(_assemble_report_items, records)

    model_name = None
    try:
        if settings.model_path_abs:
            model_name = settings.model_path_abs.name
    except Exception:
        pass

    def _build() -> bytes:
        from app.core.report import build_report_pdf

        return build_report_pdf(items, meta={
            "model_name": model_name,
            "conf": records[0].get("conf_threshold"),
            "batch_count": len(items),
        })

    try:
        pdf_bytes = await run_in_threadpool(_build)
    except Exception as exc:
        raise AppError(f"报告生成失败: {exc}", code="REPORT_FAILED", status_code=500)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"detection_report_{stamp}_{len(items)}pcs.pdf"
    return _attachment_response(
        pdf_bytes, filename, "application/pdf", {"X-Report-Images": str(len(items))}
    )


@report_router.post("/export", summary="导出检测标签 (CSV / COCO / YOLO)")
async def export_labels_api(
    payload: Dict[str, Any] = Body(
        ..., description='{"record_ids": [1,2], "format": "csv | coco | yolo"}'
    ),
) -> Response:
    """把检测结果导出为下游训练可直接消费的标注数据。

    请求体:
        {"record_ids": [101, 102], "format": "csv" | "coco" | "yolo"}
        - csv : 一行一个目标框, 含归一化坐标, Excel 可直接打开
        - coco: 标准 COCO detection JSON (category_id 为 1-based)
        - yolo: zip 包, 内含 labels/*.txt + classes.txt + README.txt
    """
    body = payload or {}
    ids = body.get("record_ids") or []
    fmt = str(body.get("format") or "").strip().lower()

    if not isinstance(ids, list) or not ids:
        raise AppError("请提供要导出的记录 ID 列表", code="EMPTY_RECORDS")
    if fmt not in exporter.FORMATS:
        raise AppError(
            f"不支持的导出格式: {fmt or '(空)'}, 可选 {' / '.join(exporter.FORMATS)}",
            code="INVALID_FORMAT",
        )
    if len(ids) > _MAX_REPORT_RECORDS:
        raise AppError(
            f"单次最多导出 {_MAX_REPORT_RECORDS} 张影像的标签, 当前 {len(ids)} 张",
            code="TOO_MANY_RECORDS",
        )

    records = _load_records(ids, "导出标签")

    def _build() -> Tuple[bytes, str, str]:
        return exporter.export_labels(_assemble_report_items(records), fmt)

    try:
        content, ext, media_type = await run_in_threadpool(_build)
    except Exception as exc:
        raise AppError(f"标签导出失败: {exc}", code="EXPORT_FAILED", status_code=500)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"detection_labels_{stamp}_{len(records)}pcs{ext}"
    return _attachment_response(content, filename, media_type, {"X-Export-Format": fmt})
