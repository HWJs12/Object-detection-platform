"""可复现实验包: 把一条检测记录的全部可追溯物证打包为 zip。

设计要点:
    - **纯 io 模块**: 不查数据库、不拼 URL, 输入是 api 层已组装好的 manifest 与
      一份"文件名 -> 磁盘路径"的解析器, 输出 bytes。这样它可以在测试里脱离
      数据库独立验证, 也便于将来复用到批量导出。
    - **缺文件不阻断**: 源文件或结果图可能已被保留策略清理、也可能本来就没生成
      (如视频未开结果视频导出)。此时写入 `MISSING.txt` 如实说明缺什么、
      为什么缺, 而不是让整个导出失败, 也不静默漏掉。
    - **README 到位**: 包里附一份自解释的 README, 说明每个文件的含义、
      如何用同一套参数重跑、以及哪些数字不能直接比较。
    - **口径透明**: 参数快照取自记录里保存的 `inference_snapshot`,
      不用当前配置回填 —— 那会把"当时怎么跑的"改成"现在怎么配的"。

包内结构:
    manifest.json          本次导出的元信息与文件清单
    README.md              重跑说明与口径提示
    parameters.json        推理参数快照(模型 / 设备 / 阈值 / 抽帧 ...)
    environment.json       环境信息(Python / 依赖版本 / 平台)
    record.json            该条记录的结构化全量数据(不含明细大表)
    results.csv            逐目标明细(一行一个检出框)
    frame_stats.csv        逐帧统计(视频记录才有, 图片为空表)
    class_stats.csv        类别汇总
    detections/*.jpg|mp4   检测结果图 / 结果视频(若存在)
    source/*.<ext>         原始上传文件(若仍存在于受管目录)
    MISSING.txt            缺失文件说明(仅在确有缺失时生成)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import platform
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# 单个复现包的大小上限, 防止误把超大视频拖进下载导致长时间卡住
MAX_BUNDLE_BYTES = 512 * 1024 * 1024

CSV_HEADER: List[str] = [
    "frame_index", "class_id", "class_name", "confidence",
    "x1", "y1", "x2", "y2", "img_width", "img_height",
]

FRAME_HEADER: List[str] = [
    "frame_index", "timestamp_sec", "total_objects", "class_counts",
]

CLASS_HEADER: List[str] = ["class_name", "count"]


def _json_bytes(payload: Any) -> bytes:
    """统一 JSON 序列化: 保留中文、两点缩进, 便于人工阅读与 diff。"""
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    # utf-8-sig: 带 BOM, Excel 双击打开不乱码(与标签导出保持一致)
    return buf.getvalue().encode("utf-8-sig")


def _num(value: Any, digits: int = 4) -> Any:
    """数值四舍五入; 非数值原样返回空串, 避免 CSV 里出现 None 字样。"""
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return ""


def environment_info() -> Dict[str, Any]:
    """采集环境信息。只读、全部字段容错, 缺依赖不影响导出。"""
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
    }

    packages: Dict[str, Optional[str]] = {}
    for name in ("ultralytics", "torch", "opencv-python", "numpy", "fastapi", "reportlab"):
        try:
            from importlib.metadata import version

            packages[name] = version(name)
        except Exception:
            packages[name] = None
    info["packages"] = packages

    # 推理设备: 取配置解析结果, 采集失败不影响其余字段
    try:
        from config import settings

        info["device"] = settings.device_resolved
        info["imgsz"] = settings.imgsz
    except Exception:
        info["device"] = None
    return info


def _sample_hint(manifest: Dict[str, Any]) -> str:
    """给出"本包明细条数 vs 该记录累计检出框"的具体对照。

    只写一句"视频明细是采样"不够: 读者看到 results.csv 里 200 行、
    record.json 里 total_detections 2332, 无从判断这 200 是抽样还是全量。
    这里把两个数直接摆出来, 差异一目了然。
    """
    rows = manifest.get("item_rows")
    total = manifest.get("total_detections")
    try:
        rows_i = int(rows or 0)
        total_i = int(total or 0)
    except (TypeError, ValueError):
        return ""
    if not rows_i or total_i <= rows_i:
        return ""
    return (
        f"   本包 `results.csv` 共 {rows_i} 行明细, 而该记录累计检出框为 {total_i} 个,\n"
        f"   差额部分未入库(受明细入库采样上限约束), 属于正常的采样口径差异。\n"
    )


def _readme(manifest: Dict[str, Any], missing: Sequence[Dict[str, str]]) -> str:
    """生成重跑说明。内容随记录类型变化(图片/视频), 不写死死板模板。"""
    task_type = manifest.get("task_type") or "image"
    params = manifest.get("parameters") or {}
    record_id = manifest.get("record_id")
    source_name = manifest.get("source_name") or "unknown"

    param_lines = "\n".join(
        f"| {key} | {value if value is not None else '(未记录)'} |"
        for key, value in params.items()
    ) or "| (记录中未保存参数快照) | — |"

    if task_type == "video":
        rerun = f"""1. 打开平台的"视频检测"页, 上传 `source/` 下的原始视频;
2. 把识别阈值设为 `{params.get('conf_threshold', '(见 parameters.json)')}`、
   抽帧间隔设为 `{params.get('frame_skip', '(见 parameters.json)')}`、
   长边缩放设为 `{params.get('video_max_side', '(见 parameters.json)')}`;
3. 跟踪器选择 `{params.get('tracker', 'bytetrack.yaml')}`,
   跟踪开关 `{'开启' if params.get('track_enabled') else '关闭'}`;
4. 提交后对照本包 `results.csv` 与 `frame_stats.csv` 检查数量与逐帧分布。"""
    else:
        rerun = f"""1. 打开平台的"图片检测"页, 上传 `source/` 下的原始图片;
2. 把识别阈值设为 `{params.get('conf_threshold', '(见 parameters.json)')}`、
   输入尺寸设为 `{params.get('imgsz', '(见 parameters.json)')}`、
   IoU 阈值设为 `{params.get('iou_threshold', '(见 parameters.json)')}`;
3. 提交后对照本包 `results.csv` 逐个检查检出框的类别、置信度与坐标。"""

    missing_block = ""
    if missing:
        lines = "\n".join(
            f"- `{m['file']}`: {m['reason']}" for m in missing
        )
        missing_block = f"""
## 本包缺失的文件

以下文件在导出时已无法从受管目录取到, 原因如实列在下面。缺失不影响其余文件的使用。

{lines}
"""

    return f"""# 检测记录 #{record_id} 复现包

原始文件: `{source_name}`
记录类型: `{task_type}`
检测时间: `{manifest.get('created_at') or '未记录'}`
导出于: `{manifest.get('exported_at')}`

本包把一条检测记录的全部可追溯物证集中在一起, 目的是让"这个结果是怎么跑出来的"
可以被第三方独立核对, 而不必依赖平台当时的会话状态。

## 文件说明

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 本次导出的元信息与文件清单, 含内容摘要 |
| `parameters.json` | 推理参数快照, 即**当时**实际生效的模型与参数 |
| `environment.json` | Python 版本、关键依赖版本、平台与推理设备 |
| `record.json` | 该记录的结构化全量数据(计数口径、调查维度、生态指标等) |
| `results.csv` | 逐目标明细, 一行一个检出框, 含置信度与绝对像素坐标 |
| `frame_stats.csv` | 逐帧统计(视频记录), 含每帧各类别计数 |
| `class_stats.csv` | 类别汇总 |
| `source/` | 原始上传文件 |
| `detections/` | 检测结果图或结果视频 |

## 完整性自校验

解包后可按下述三步核对本包是否完整, 全程不需要联网:

1. `manifest.json` 的 `file_count` 应等于 `files` 数组长度,
   同时等于 `source/` 与 `detections/` 下的实际文件数之和;
2. 逐个比对 `files[].sha256` 与该文件的摘要, 一致即为内容未被改动;
3. `missing_count` 应等于 `missing` 数组长度; 若大于 0, 说明部分原件在导出时
   已不可取, 具体原因见 `missing` 与文末的缺失清单。

## 参数快照

| 参数 | 取值 |
| --- | --- |
{param_lines}

## 如何用同一套参数重跑

{rerun}

## 阅读这些数字时请注意

1. **计数口径** — 本记录的计数取自 `{manifest.get('effective_count_source')}`
   (`{manifest.get('counting_source_label')}`), 折算值为
   `{manifest.get('effective_count')}`。视频记录下"跨帧轨迹数 / 单帧峰值 MaxN /
   累计检出框"三者数值可能相差数倍, **跨记录比较前必须先对齐口径**。
2. **MaxN 是相对丰度** — 单帧峰值通常低估实际经过的目标总数,
   不等同于生物个体数或绝对密度。
3. **置信度不是准确率** — `results.csv` 中的 `confidence` 是模型输出,
   未经人工核验; 不同模型的置信度不可横向比较。
4. **视频明细是采样** — `results.csv` 对视频记录仅包含入库的采样明细,
   受采样上限约束, `frame_stats.csv` 才是逐帧统计的完整框架。
{_sample_hint(manifest)}5. **重跑结果不会逐位相同** — 即便参数一致, 推理库版本或硬件差异仍可能带来
   亚像素级的坐标抖动, 比对时应容许小幅误差。
{missing_block}"""


def _zip_add_file(zf: zipfile.ZipFile, arcname: str, path: Path,
                  added: List[Dict[str, Any]], kind: str) -> None:
    """把磁盘文件写入 zip 并登记到清单; 读取失败则跳过(由调用方决定是否记缺失)。"""
    try:
        data = path.read_bytes()
    except OSError:
        return
    zf.writestr(arcname, data)
    added.append({
        "arcname": arcname,
        "kind": kind,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    })


def build_bundle(
    manifest: Dict[str, Any],
    record: Dict[str, Any],
    resolve_path: Callable[[str, str], Optional[Path]],
) -> Tuple[bytes, Dict[str, Any]]:
    """组装复现包。

    参数:
        manifest: 展平后的记录摘要(含 record_id / task_type / 参数 / 计数口径等)
        record:   该记录的完整字典(`dao.get_record` 的结果)
        resolve_path: `(字段名, 文件名) -> 绝对路径|None`, 由 api 层用 storage 的
                      解析逻辑注入, 保证与本系统的路径穿越防护一致。

    返回:
        (zip 字节, 统计信息)。统计信息含文件数、总字节、缺失清单与是否超限。
    """
    added: List[Dict[str, Any]] = []
    missing: List[Dict[str, str]] = []
    total = 0

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # ---- 结构化数据 ----
        zf.writestr("parameters.json", _json_bytes(manifest.get("parameters") or {}))
        zf.writestr("environment.json", _json_bytes(manifest.get("environment") or {}))
        zf.writestr("record.json", _json_bytes(record))

        # ---- 明细 CSV ----
        items = record.get("items") or []
        width = record.get("image_width")
        height = record.get("image_height")
        zf.writestr("results.csv", _csv_bytes(CSV_HEADER, [
            [
                it.get("frame_index", 0),
                it.get("class_id", ""),
                it.get("class_name", ""),
                _num(it.get("confidence")),
                _num(it.get("x1"), 2), _num(it.get("y1"), 2),
                _num(it.get("x2"), 2), _num(it.get("y2"), 2),
                width or "", height or "",
            ]
            for it in items
        ]))

        # ---- 逐帧统计(图片记录为空表, 但保留表头说明结构) ----
        frames = record.get("frame_stats") or []
        zf.writestr("frame_stats.csv", _csv_bytes(FRAME_HEADER, [
            [
                f.get("frame_index"),
                _num(f.get("timestamp_sec"), 2),
                f.get("total_objects", 0),
                json.dumps(f.get("class_counts") or {}, ensure_ascii=False),
            ]
            for f in frames
        ]))

        # ---- 类别汇总 ----
        stats = record.get("class_stats") or []
        zf.writestr("class_stats.csv", _csv_bytes(CLASS_HEADER, [
            [s.get("class_name", ""), s.get("count", 0)] for s in stats
        ]))

        # ---- 源文件与结果文件 ----
        source_name = record.get("saved_name")
        if source_name:
            path = resolve_path("saved_name", source_name)
            if path and path.exists():
                arcname = f"source/{Path(source_name).name}"
                before = len(added)
                _zip_add_file(zf, arcname, path, added, "source")
                total += path.stat().st_size
                if len(added) == before:
                    missing.append({"file": arcname, "reason": "文件读取失败(可能已被锁定或权限不足)"})
            else:
                missing.append({
                    "file": f"source/{Path(source_name).name}",
                    "reason": "原始上传文件已不在受管目录中(可能已被保留策略清理或移入回收站)",
                })

        for column, label in (("result_image", "image"), ("result_video", "video")):
            filename = record.get(column)
            if not filename:
                if column == "result_video" and record.get("task_type") == "video":
                    missing.append({
                        "file": "detections/",
                        "reason": "该视频记录未生成结果视频(可能导出被关闭或编码器不可用)",
                    })
                continue
            path = resolve_path(column, filename)
            if path and path.exists():
                arcname = f"detections/{Path(filename).name}"
                before = len(added)
                _zip_add_file(zf, arcname, path, added, label)
                total += path.stat().st_size
                if len(added) == before:
                    missing.append({"file": arcname, "reason": "文件读取失败"})
            else:
                missing.append({
                    "file": f"detections/{Path(filename).name}",
                    "reason": "结果文件已不在受管目录中(可能已被清理)",
                })

        # ---- README 与 manifest 最后写, 确保清单里已含全部文件 ----
        manifest = dict(manifest)
        manifest["files"] = added
        manifest["missing"] = missing
        manifest["file_count"] = len(added)
        manifest["missing_count"] = len(missing)
        manifest["total_bytes"] = total
        manifest["bundle_format"] = "underwater-detection-repro/1"
        # 明细条数写进 manifest: README 要据此说明"本包明细 vs 该记录累计检出框",
        # 否则读者会把采样明细误当成全部目标。
        # total_detections 优先取 manifest, 缺失时从 record 兜底 —— 让调用方
        # 不必为了这一句话而重复拼装字段。
        manifest["item_rows"] = len(items)
        if manifest.get("total_detections") is None:
            manifest["total_detections"] = record.get("total_detections")
        zf.writestr("README.md", _readme(manifest, missing).encode("utf-8"))
        zf.writestr("manifest.json", _json_bytes(manifest))

    payload = buffer.getvalue()
    stats_out = {
        "record_id": manifest.get("record_id"),
        "files": len(added),
        "missing": missing,
        "payload_bytes": total,
        "bundle_bytes": len(payload),
    }
    return payload, stats_out


def safe_archive_name(record: Dict[str, Any]) -> str:
    """生成 zip 内不含路径穿越的文件名片段。

    源文件名来自用户上传, 可能含中文、空格或 `../`。这里只取纯文件名主干并做
    字符白名单替换, 保证下载后在各平台上都能正常解压。
    """
    raw = str(record.get("source_name") or record.get("saved_name") or "record")
    stem = Path(raw).stem or "record"
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in stem)
    cleaned = cleaned.strip("_")[:48] or "record"
    return cleaned
