"""历史记录接口: 查询 / 详情 / 删除 / 检测报告导出。"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.core import exporter, repro
from app.core.storage import (
    checkpoint_db,
    release_record_files,
    resolve_record_file,
)
from app.db import dao
from app.utils.exceptions import AppError, RecordNotFoundError
from app.utils.response import ok

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
    site_id: Optional[int] = Query(None, ge=1, description="地图站点编号"),
) -> Dict[str, Any]:
    rows, total = dao.list_records(
        task_type=task_type, keyword=keyword, date_from=date_from,
        date_to=date_to, page=page, page_size=page_size, site_id=site_id,
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
            "record_id": rec.get("id"),
            "source_hash": rec.get("source_hash"),
            "inference_snapshot": rec.get("inference_snapshot") or {},
            "counting_mode": rec.get("counting_mode"),
            # 调查维度: 供报告方法学附录汇总"本批在哪些站点、什么水深下采集"。
            # 未填写时保持 None, 附录会显示"未标注", 不用空串冒充有效值。
            "site": rec.get("site"),
            "transect": rec.get("transect"),
            "depth_m": rec.get("depth_m"),
            "survey_round": rec.get("survey_round"),
            # 计数相关: 附录需要说明本批实际采用了哪套口径
            "maxn_total": rec.get("maxn_total"),
            "cpunx": rec.get("cpunx"),
            "unique_count_valid": rec.get("unique_count_valid"),
        }
        # 明细: 数据库按 frame_index/confidence 排序, 图片记录的 frame_index 恒为 0
        dets = dao.get_record_items(rec["id"], limit=None)
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
    quoted = urllib.parse.quote(filename, safe="")
    # HTTP headers require an ASCII fallback; the UTF-8 parameter keeps the real name.
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    headers: Dict[str, str] = {
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}",
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
            if rec.get("task_type") != "image":
                raise AppError("图片报告和训练标签仅支持图片记录, 视频明细为采样数据", code="IMAGE_RECORD_REQUIRED")
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

    def _build() -> bytes:
        from app.core.report import build_report_pdf

        return build_report_pdf(items, meta={
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


# ---------------------------------------------------------------- 可复现实验包

_COUNT_SOURCE_LABEL = {
    "unique_tracks": "跨帧轨迹数",
    "maxn": "单帧峰值 MaxN",
    "total_detections": "累计检出框",
    "image_objects": "图片检出框",
}

# ------------------------------------------------------------ 导出预检 (只读)
#
# 导出类接口原本都是"点下去才开始算", 有问题只能等失败后看报错。
# 预检把"能不能导、导出来大概多大、有什么后果"提前算清楚:
# 只读库与文件 stat, 不生成任何产物、不落盘、不写审计, 因此可以随便调用。

_BUNDLE_SIZE_LIMIT = repro.MAX_BUNDLE_BYTES


@report_router.get(
    "/preflight/{record_id}", summary="导出前预检 (只读, 不生成文件)"
)
async def export_preflight(record_id: int) -> Dict[str, Any]:
    """告诉前端"这条记录导出会遇到什么", 让用户在真正点击前就知道结果。

    返回:
        - 源文件 / 结果文件是否还在受管目录(已被清理的要提前告知, 而不是等 README 里才发现);
        - 打包后的大致体积, 以及素材体积是否已逼近包上限;
        - 明细行数与该记录累计检出框数的关系(视频明细有入库上限, 二者会不一致);
        - 本次导出实际采用哪套计数口径, 以及需要提醒的条目。

    副作用: 无。只读数据库字段与文件 `stat()`, 不读文件内容、不组装 zip。
    """
    record = dao.get_record(record_id)
    if not record:
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    notes: List[str] = []
    task_type = record.get("task_type") or "image"

    # ---- 素材可用性: 与 build_bundle 的取材顺序一致, 但只 stat 不读 ----
    assets: List[Dict[str, Any]] = []
    for column, label in (
        ("saved_name", "原始上传文件"),
        ("result_image", "结果图"),
        ("result_video", "结果视频"),
    ):
        filename = record.get(column)
        if not filename:
            continue
        path = resolve_record_file(column, str(filename))
        present = bool(path and path.exists())
        size = path.stat().st_size if present else 0
        assets.append({
            "field": column,
            "label": label,
            "available": present,
            "bytes": size,
        })
        if not present:
            notes.append(
                f"{label}已不在受管目录中(可能被保留策略清理或移入回收站), "
                f"复现包里会把它记入缺失清单而不是用空文件顶替"
            )

    payload_bytes = sum(a["bytes"] for a in assets if a["available"])
    # 压缩率无法预知其数值, 这里只声明一个"包大小不超过素材总量"的上界:
    # 已压缩的 jpg/mp4 接近 1:1, 明细 CSV 才会明显变小。不给出预估数字, 避免与实测不符。
    size_blocked = bool(payload_bytes) and payload_bytes > _BUNDLE_SIZE_LIMIT
    if size_blocked:
        notes.append(
            f"素材合计 {payload_bytes / 1024 / 1024:.1f} MB 已超过复现包上限 "
            f"{_BUNDLE_SIZE_LIMIT / 1024 / 1024:.0f} MB, 直接导出会被拒绝"
        )

    # ---- 明细规模: 视频明细有入库上限, 与累计检出框数不是一回事 ----
    item_rows = len(dao.get_record_items(record_id, limit=None))
    total_detections = record.get("total_detections")
    if task_type == "video" and total_detections and total_detections > item_rows:
        notes.append(
            f"results.csv 只含入库的 {item_rows} 行采样明细, 该记录累计检出框 "
            f"{total_detections} 个; 逐帧全貌以 frame_stats.csv 为准"
        )

    # ---- 计数口径: 跨记录比较前必须先对齐的那一组 ----
    source = record.get("effective_count_source") or (
        "image_objects" if task_type != "video" else "total_detections"
    )
    count_warning = None
    if task_type == "video" and source == "total_detections":
        count_warning = (
            "该视频既未取得完整去重依据, 也未产生 MaxN, 计数回退为累计检出框, "
            "与其他视频记录的计数不可直接比较"
        )
        notes.append(count_warning)

    # ---- 标签导出适用性: 标签只能对应"一张图" ----
    label_formats = ["csv", "coco", "yolo"]
    label_ok = task_type != "video"
    if not label_ok:
        label_formats = []
        notes.append(
            "视频记录不做标签导出: 一份标签只能对应一张静态影像, "
            "视频明细跨帧、含轨迹, 转成单图标签会失真"
        )

    return ok({
        "record_id": record_id,
        "task_type": task_type,
        "source_name": record.get("source_name"),
        "status": record.get("status"),
        "assets": assets,
        "asset_bytes": payload_bytes,
        "bundle_size_limit": _BUNDLE_SIZE_LIMIT,
        # 布尔字段名即结论, 不让前端自己从字节数反推判断规则
        "bundle_exportable": not size_blocked and record.get("status") == "success",
        "label_formats": label_formats,
        "label_exportable": label_ok and record.get("status") == "success",
        "item_rows": item_rows,
        "total_detections": total_detections,
        "effective_count": record.get("effective_count"),
        "effective_count_source": source,
        "counting_source_label": _COUNT_SOURCE_LABEL.get(source, source),
        "maxn_total": (record.get("maxn") or {}).get("maxn_total"),
        "notes": notes,
        "warning_count": len(notes),
    })


def _bundle_manifest(record: Dict[str, Any]) -> Dict[str, Any]:
    """把记录压成复现包所需的摘要, 只放"重跑和对账"真正需要的字段。

    刻意不含 result_url 等会话相关路径: 复现包应当能脱离本机服务被读懂。
    """
    snapshot = record.get("inference_snapshot") or {}
    source = record.get("effective_count_source") or (
        "image_objects" if record.get("task_type") != "video" else "total_detections"
    )
    return {
        "record_id": record.get("id"),
        "source_name": record.get("source_name"),
        "task_type": record.get("task_type"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "exported_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "image_width": record.get("image_width"),
        "image_height": record.get("image_height"),
        "duration_sec": record.get("duration_sec"),
        "source_hash": record.get("source_hash"),
        "conf_threshold": record.get("conf_threshold"),
        "inference_ms": record.get("inference_ms"),
        "counting_mode": record.get("counting_mode"),
        "counted_as": record.get("counted_as"),
        "counting_mode_reason": record.get("counting_mode_reason"),
        "parameters": snapshot.get("parameters") or {},
        # 计数与口径: 阅读数字前必须先对齐这一组
        "effective_count": record.get("effective_count"),
        "effective_count_source": source,
        "counting_source_label": _COUNT_SOURCE_LABEL.get(source, source),
        "total_detections": record.get("total_detections"),
        "tracked_objects": record.get("tracked_objects"),
        "maxn_total": (record.get("maxn") or {}).get("maxn_total"),
        "cpunx": (record.get("maxn") or {}).get("cpunx"),
        # 调查维度
        "site": record.get("site"),
        "transect": record.get("transect"),
        "depth_m": record.get("depth_m"),
        "survey_round": record.get("survey_round"),
        "environment": repro.environment_info(),
    }


@report_router.get("/repro/{record_id}", summary="导出可复现实验包 (zip)")
async def export_repro_bundle(record_id: int) -> Response:
    """把一条记录的全部物证打包成 zip: 源文件 / 参数快照 / 明细 / 逐帧统计 /
    模型摘要 / 环境信息 / README。

    仅在调用时组装, 不落盘。缺失的文件(已被保留策略清理等)会在 README 与
    manifest 中如实列出, 不以空文件冒充。
    """
    record = dao.get_record(record_id)
    if not record:
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    manifest = _bundle_manifest(record)
    record["items"] = dao.get_record_items(record_id, limit=None)
    record["class_stats"] = dao.get_record_class_stats(record_id)
    record["frame_stats"] = dao.get_record_frame_stats(record_id)

    def _build() -> Tuple[bytes, Dict[str, Any]]:
        return repro.build_bundle(manifest, record, resolve_record_file)

    try:
        bundle, stats_out = await run_in_threadpool(_build)
    except Exception as exc:
        raise AppError(f"复现包生成失败: {exc}", code="REPRO_FAILED", status_code=500)

    if len(bundle) > repro.MAX_BUNDLE_BYTES:
        raise AppError(
            f"复现包体积 {len(bundle) / 1024 / 1024:.1f} MB 超过上限 "
            f"{repro.MAX_BUNDLE_BYTES / 1024 / 1024:.0f} MB, 请先确认源视频大小",
            code="BUNDLE_TOO_LARGE",
        )

    stem = repro.safe_archive_name(record)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"repro_{record_id}_{stem}_{stamp}.zip"
    return _attachment_response(bundle, filename, "application/zip", {
        "X-Repro-Files": str(stats_out["files"]),
        "X-Repro-Missing": str(len(stats_out["missing"])),
    })
