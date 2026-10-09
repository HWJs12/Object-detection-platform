"""检测报告 PDF 生成(按需生成, 不落盘缓存)。

设计要点:
    - **懒生成**: 只在用户点击"下载报告"时调用, 生成到内存 BytesIO 直接回传,
      不在磁盘留任何中间文件 —— 请求结束即释放, 不占长期内存/磁盘。
    - **按批次组织**: 一次上传 N 张图片 -> 一份 PDF 含 N 个逐图分析页 + 1 个汇总页。
    - **结论由规则推导**: 基于检出数量、类别分布、置信度水平自动生成中文分析结论,
      不做无依据的臆测表述。
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from typing import Any, Dict, List, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.utils.image_utils import draw_detections, imdecode
from config import settings

# ---------------- 字体 ----------------
_FONT_NAME = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONT_READY = False

# 优先使用系统自带中文字体(按观感排序), 全部缺失时退回内置字体
_FONT_CANDIDATES: Sequence[tuple[str, str, str]] = (
    ("MSYH", "msyh.ttc", "msyhbd.ttc"),
    ("SimHei", "simhei.ttf", "simhei.ttf"),
    ("Deng", "Deng.ttf", "Dengb.ttf"),
    ("SimSun", "simsun.ttc", "simsun.ttc"),
)


def _font_dir():
    for candidate in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts"), Path("/System/Library/Fonts")):
        if candidate.exists():
            return candidate
    return None


def _ensure_font() -> tuple[str, str]:
    """注册中文字体, 返回 (常规字体名, 粗体字体名)。"""
    global _FONT_NAME, _FONT_BOLD, _FONT_READY
    if _FONT_READY:
        return _FONT_NAME, _FONT_BOLD

    base = _font_dir()
    if base is not None:
        for name, regular, bold in _FONT_CANDIDATES:
            regular_path = base / regular
            if not regular_path.exists():
                continue
            try:
                pdfmetrics.registerFont(TTFont(name, str(regular_path)))
                bold_path = base / bold
                bold_name = name
                if bold_path.exists() and bold_path != regular_path:
                    pdfmetrics.registerFont(TTFont(name + "-Bold", str(bold_path)))
                    bold_name = name + "-Bold"
                _FONT_NAME, _FONT_BOLD = name, bold_name
            except Exception:
                continue
            else:
                break

    _FONT_READY = True
    return _FONT_NAME, _FONT_BOLD


# ---------------- 配色 ----------------
_C_PRIMARY = colors.HexColor("#0e7490")
_C_TEXT = colors.HexColor("#1f2937")
_C_MUTED = colors.HexColor("#6b7280")
_C_BORDER = colors.HexColor("#d1d5db")
_C_HEAD_BG = colors.HexColor("#e0f2f7")
_C_ZEBRA = colors.HexColor("#f8fafc")
_C_WARN = colors.HexColor("#b45309")
_C_OK = colors.HexColor("#15803d")


def _styles() -> Dict[str, ParagraphStyle]:
    font, bold = _ensure_font()
    return {
        "title": ParagraphStyle("title", fontName=bold, fontSize=20, leading=26,
                                textColor=_C_PRIMARY, alignment=TA_CENTER, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName=font, fontSize=10, leading=15,
                                   textColor=_C_MUTED, alignment=TA_CENTER, spaceAfter=14),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=13, leading=19,
                             textColor=_C_PRIMARY, spaceBefore=8, spaceAfter=6),
        "h3": ParagraphStyle("h3", fontName=bold, fontSize=11, leading=16,
                             textColor=_C_TEXT, spaceBefore=6, spaceAfter=4),
        "body": ParagraphStyle("body", fontName=font, fontSize=9.5, leading=15,
                               textColor=_C_TEXT),
        "small": ParagraphStyle("small", fontName=font, fontSize=8.5, leading=13,
                                textColor=_C_MUTED),
        "concl": ParagraphStyle("concl", fontName=font, fontSize=9.5, leading=16,
                                textColor=_C_TEXT, alignment=TA_LEFT,
                                leftIndent=6, rightIndent=6, spaceBefore=4, spaceAfter=4),
    }


# ---------------- 结论生成 ----------------


def _analyse(item: Dict[str, Any], batch_avg_conf: float | None = None) -> List[str]:
    """基于单张图检测结果推导中文分析结论(规则驱动, 不臆测)。"""
    lines: List[str] = []
    total = int(item.get("total") or 0)
    counts: List[Dict[str, Any]] = item.get("counts") or []
    dets: List[Dict[str, Any]] = item.get("detections") or []

    if not total:
        lines.append("本图未检出任何目标。可能原因: 目标尺寸小于模型感受野、置信度阈值偏高, "
                     "或画面中确实不存在训练类别对应的生物。建议尝试将置信度阈值下调后复检。")
        return lines

    # 1) 数量与密度
    width = item.get("width") or 0
    height = item.get("height") or 0
    density = ""
    if width and height:
        per_mp = total / (width * height / 1_000_000)
        density = f", 约 {per_mp:.1f} 个 / 百万像素"
    lines.append(f"本图共检出 {total} 个目标{density}, 属{'高' if total >= 15 else '中' if total >= 5 else '低'}"
                 f"密度场景。")

    # 2) 类别构成
    if counts:
        top = sorted(counts, key=lambda c: -c["count"])
        head, *rest = top
        share = head["count"] / total * 100 if total else 0
        desc = f"以「{head['class_name']}」为主, 计 {head['count']} 个, 占 {share:.0f}%"
        if rest:
            others = ", ".join(f"{c['class_name']} {c['count']} 个" for c in rest[:4])
            desc += f"; 其余为 {others}"
        lines.append(desc + f"。共出现 {len(top)} 个类别, "
                     + ("类别组成较单一, 群落结构集中。" if len(top) == 1
                        else "类别多样性尚可, 呈现多物种共存特征。" if len(top) <= 3
                        else "类别组成丰富, 群落多样性高。"))

    # 3) 置信度水平
    if dets:
        confs = [float(d.get("confidence") or 0) for d in dets]
        avg = sum(confs) / len(confs)
        high = sum(1 for c in confs if c >= 0.7)
        low = sum(1 for c in confs if c < 0.4)
        level = "高" if avg >= 0.7 else "中等" if avg >= 0.5 else "偏低"
        lines.append(f"检测置信度均值 {avg:.3f}, 整体可信度{level}; "
                     f"其中 {high} 个目标的置信度达到 0.70 以上, "
                     f"{low} 个低于 0.40。")
        if low:
            lines.append(f"存在 {low} 个低置信度目标, 多见于目标遮挡、水体浑浊或光照不足导致的"
                         f"轮廓不完整, 建议结合原始影像人工复核。")

    # 4) 尺寸分布
    if dets:
        areas = []
        for d in dets:
            bbox = d.get("bbox") or [0, 0, 0, 0]
            areas.append(max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])))
        if areas and width and height:
            frame_area = width * height
            small = sum(1 for a in areas if a / frame_area < 0.01)
            if small:
                lines.append(f"有 {small} 个目标占画面面积不足 1%, 属小目标检测, "
                             f"是该场景下的主要难度来源。")

    if batch_avg_conf is not None and dets:
        item_avg = sum(float(d.get("confidence") or 0) for d in dets) / len(dets)
        if item_avg - batch_avg_conf > 0.08:
            lines.append("本图置信度高于本批次平均水平, 成像质量较好。")
        elif batch_avg_conf - item_avg > 0.08:
            lines.append("本图置信度低于本批次平均水平, 成像条件相对不利。")

    return lines


def _batch_conclusion(items: Sequence[Dict[str, Any]]) -> List[str]:
    """整批汇总结论。"""
    ok = [it for it in items if not it.get("error")]
    failed = [it for it in items if it.get("error")]
    total_objects = sum(int(it.get("total") or 0) for it in ok)

    lines: List[str] = []
    lines.append(f"本次共处理 {len(items)} 张影像, 成功 {len(ok)} 张"
                 + (f", 失败 {len(failed)} 张" if failed else "")
                 + f", 累计检出目标 {total_objects} 个, "
                 f"平均每张 {total_objects / len(ok):.1f} 个。" if ok else "本次无有效检测结果。")

    if not ok:
        return lines

    # 类别总览
    agg: Dict[str, int] = {}
    for it in ok:
        for c in it.get("counts") or []:
            agg[c["class_name"]] = agg.get(c["class_name"], 0) + int(c["count"])
    if agg:
        ranked = sorted(agg.items(), key=lambda kv: -kv[1])
        share = ranked[0][1] / total_objects * 100 if total_objects else 0
        lines.append("全批次类别分布: "
                     + ", ".join(f"{k} {v} 个" for k, v in ranked)
                     + f"。其中优势类别为「{ranked[0][0]}」, 占比 {share:.0f}%。")

    # 逐图产量
    per_image = [(it.get("source_name") or "-", int(it.get("total") or 0)) for it in ok]
    if len(per_image) > 1:
        best = max(per_image, key=lambda x: x[1])
        worst = min(per_image, key=lambda x: x[1])
        lines.append(f"单图检出量区间为 {worst[1]} ~ {best[1]} 个, 最高为「{best[0]}」"
                     f"({best[1]} 个), 最低为「{worst[0]}」({worst[1]} 个)。")

    # 质量评估
    confs = [float(d.get("confidence") or 0)
             for it in ok for d in (it.get("detections") or [])]
    if confs:
        avg = sum(confs) / len(confs)
        lines.append(f"全批次平均置信度 {avg:.3f}, "
                     + ("检测结果可信度高, 可作为统计依据。" if avg >= 0.7
                        else "检测结果整体可信, 建议对低置信度目标人工抽检。" if avg >= 0.5
                        else "平均置信度偏低, 建议核查成像质量或调整置信度阈值后重新检测。"))

    # 相似图提示(同源图片重复上传)
    names = [it.get("source_name") or "" for it in ok]
    if len(names) != len(set(names)):
        dupes = {n for n in names if names.count(n) > 1}
        lines.append(f"注意: 本批含 {len(dupes)} 个同名文件, 可能导致同一目标被重复统计, "
                     f"汇总数据解读时需留意。")

    if failed:
        lines.append(f"另有 {len(failed)} 张影像检测失败: "
                     + "; ".join(f"{it.get('source_name') or '-'}({it.get('error')})"
                                 for it in failed[:3])
                     + ("..." if len(failed) > 3 else "") + "。")

    lines.append(f"结论: 本次检测覆盖 {len(ok)} 张水下影像, 共识别 {total_objects} 个目标, "
                 f"涉及 {len(agg)} 个类别, 检测流程完整执行。")
    return lines


# ---------------- PDF 构建 ----------------


def _resolve_image_path(item: Dict[str, Any]) -> Path | None:
    """定位该记录可用于报告渲染的图片路径(原图优先, 结果图兜底)。"""
    candidates = []
    original = item.get("original_url") or ""
    if original:
        candidates.append(settings.upload_dir / original.rsplit("/", 1)[-1])
    for key in ("result_image", "saved_name"):
        name = item.get(key)
        if name:
            candidates.append(settings.result_dir / Path(name).name)
            candidates.append(settings.upload_dir / Path(name).name)
    return next((p for p in candidates if p.exists()), None)


def _measure_image(item: Dict[str, Any], max_w: float, max_h: float) -> tuple[float, float]:
    """按原始宽高比预先计算图片缩放后的 (宽, 高), 供排版预算使用。

    读取失败或无图时返回 (0, 0), 调用方按无图处理。
    """
    path = _resolve_image_path(item)
    if path is None:
        return 0.0, 0.0
    try:
        image = imdecode(path.read_bytes())
    except Exception:
        return 0.0, 0.0
    if image is None:
        return 0.0, 0.0
    height, width = image.shape[:2]
    if width <= 0 or height <= 0:
        return 0.0, 0.0
    scale = min(max_w / width, max_h / height)
    return width * scale, height * scale


def _load_image_flowable(item: Dict[str, Any], max_w: float, st: Dict[str, ParagraphStyle],
                         max_h: float = 78 * mm):
    """读取原图 -> 叠加检测框 -> 返回可缩放图片对象。

    优先用原始上传图叠加检测框(可保证框与图对应); 若原图缺失(历史记录中
    saved_name 曾被误存为结果图名), 则回退到已保存的结果图, 避免报告缺图。
    max_h 限制图片最大高度, 保证「图 + 统计 + 结论」能排进同一页。
    """
    path = _resolve_image_path(item)
    if path is None:
        return None

    try:
        image = imdecode(path.read_bytes())
    except Exception:
        return None
    if image is None:
        return None

    # 结果图本身已含检测框, 避免重复绘制; 仅在原图上叠加
    is_result = path.parent == settings.result_dir
    if is_result or not (item.get("detections")):
        drawn = image
    else:
        drawn = draw_detections(image.copy(), _rebuild_detections(item))

    height, width = drawn.shape[:2]
    ok, buf = _encode_jpeg(drawn)
    if not ok:
        return None

    # 同时受限于可用宽度与可用高度, 取较小缩放比, 避免图片过高把后续内容挤到下一页
    scale = min(max_w / width, max_h / height)
    return Image(buf, width=width * scale, height=height * scale)


class _Detection:
    """供绘制复用的轻量检测对象(draw_detections 需要的字段)。"""

    __slots__ = ("class_id", "class_name", "confidence", "bbox")

    def __init__(self, class_id: int, class_name: str, confidence: float, bbox: list):
        self.class_id = class_id
        self.class_name = class_name
        self.confidence = confidence
        self.bbox = bbox


def _rebuild_detections(item: Dict[str, Any]):
    """把入库/接口返回的字典还原为可绘制的对象列表。"""
    out = []
    for d in item.get("detections") or []:
        bbox = d.get("bbox") or [0, 0, 0, 0]
        out.append(_Detection(
            class_id=int(d.get("class_id") or 0),
            class_name=str(d.get("class_name") or ""),
            confidence=float(d.get("confidence") or 0),
            bbox=[float(v) for v in bbox],
        ))
    return out


def _encode_jpeg(image) -> tuple[bool, io.BytesIO]:
    import cv2

    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return False, io.BytesIO()
    return True, io.BytesIO(buf.tobytes())


def _kv_table(rows: Sequence[Sequence[str]], st, widths=None) -> Table:
    data = [[Paragraph(f"<b>{k}</b>", st["body"]), Paragraph(str(v), st["body"])] for k, v in rows]
    table = Table(data, colWidths=widths or [52 * mm, 90 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _class_table(counts: Sequence[Dict[str, Any]], total: int, st) -> Table:
    header = ["类别", "数量", "占比", "占比示意"]
    data = [[Paragraph(f"<b>{h}</b>", st["small"] if False else st["body"]) for h in header]]
    for c in sorted(counts, key=lambda x: -x["count"]):
        pct = c["count"] / total * 100 if total else 0
        bar = "█" * max(1, int(pct / 5)) + "░" * max(0, 20 - int(pct / 5))
        data.append([
            Paragraph(str(c["class_name"]), st["body"]),
            Paragraph(str(c["count"]), st["body"]),
            Paragraph(f"{pct:.1f}%", st["body"]),
            Paragraph(f'<font color="#0e7490">{bar}</font>', st["small"]),
        ])
    table = Table(data, colWidths=[38 * mm, 22 * mm, 22 * mm, 60 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _C_HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), _C_TEXT),
        ("GRID", (0, 0), (-1, -1), 0.4, _C_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _C_ZEBRA]),
    ]))
    return table


def _detail_table(dets: Sequence[Dict[str, Any]], st, limit: int = 40,
                  font_size: float | None = None) -> Table:
    header = ["#", "类别", "置信度", "边界框 (x1, y1, x2, y2)", "尺寸"]
    data = [[Paragraph(f"<b>{h}</b>", st["small"]) for h in header]]
    for i, d in enumerate(dets[:limit]):
        bbox = d.get("bbox") or [0, 0, 0, 0]
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        data.append([
            Paragraph(str(i + 1), st["small"]),
            Paragraph(str(d.get("class_name") or ""), st["small"]),
            Paragraph(f"{float(d.get('confidence') or 0) * 100:.1f}%", st["small"]),
            Paragraph(f"{bbox[0]:.0f}, {bbox[1]:.0f}, {bbox[2]:.0f}, {bbox[3]:.0f}", st["small"]),
            Paragraph(f"{w:.0f} × {h:.0f}", st["small"]),
        ])
    if len(dets) > limit:
        data.append([Paragraph(f"<i>... 其余 {len(dets) - limit} 条已省略</i>", st["small"])] + [""] * 4)

    table = Table(data, colWidths=[10 * mm, 26 * mm, 20 * mm, 52 * mm, 26 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _C_HEAD_BG),
        ("GRID", (0, 0), (-1, -1), 0.4, _C_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _C_ZEBRA]),
    ]))
    return table


def _conclusion_box(lines: Sequence[str], st) -> Table:
    body = "<br/>".join(f"· {ln}" for ln in lines) if lines else "· 无"
    content = Paragraph(body, st["concl"])
    table = Table([[content]], colWidths=[170 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f9ff")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bae6fd")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


# ---------------- 单页排版预算 ----------------
# A4 高度 297mm, 上下页边距各 20mm, 页脚线在 15mm 处, 底部留 3mm 安全区。
_PAGE_CONTENT_H = 257 * mm      # 可用内容高度
_FRAME_W = 170 * mm             # 正文栏宽
_DETAIL_ROW_H = 6.2 * mm        # 明细表单行高度(估算上限, 实际由 wrap 测量)


def _flowable_h(flowable, avail_w: float = _FRAME_W, avail_h: float = _PAGE_CONTENT_H) -> float:
    """实测单个 flowable 的真实高度(取 wrap 结果), 失败时回退 0。"""
    if flowable is None:
        return 0.0
    try:
        _, h = flowable.wrap(avail_w, avail_h)
        return float(h)
    except Exception:
        return 0.0


def _detail_rows_that_fit(upper_h: float, dets: Sequence[Dict[str, Any]],
                          st: Dict[str, ParagraphStyle], title_h: float = 0.0) -> int:
    """给定明细表之前的实测累计高度, 求最多能放下几行明细(不含表头)。

    用二分/线性回退实测表格高度, 避免因长文本换行导致行高被低估而溢出到下一页。
    """
    avail = _PAGE_CONTENT_H - upper_h - title_h
    if avail <= 0 or not dets:
        return 0
    best = 0
    for n in range(1, min(len(dets), 14) + 1):
        if _flowable_h(_detail_table(dets, st, limit=n)) <= avail:
            best = n
        else:
            break
    return best


def _page_footer(canvas, doc) -> None:
    font, _ = _ensure_font()
    canvas.saveState()
    canvas.setFont(font, 7.5)
    canvas.setFillColor(_C_MUTED)
    canvas.drawString(20 * mm, 12 * mm, f"基于 GSCNet 的海洋检测平台 · 检测报告")
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"第 {doc.page} 页")
    canvas.setStrokeColor(_C_BORDER)
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
    canvas.restoreState()


def build_report_pdf(items: Sequence[Dict[str, Any]],
                     meta: Dict[str, Any] | None = None) -> bytes:
    """生成检测报告 PDF, 返回字节内容(调用方直接回传, 不落盘)。

    Args:
        items: 每张图片的检测结果(与 /api/detect/images 的 items 结构一致)
        meta: 可选元信息, 如 {"batch_id": ..., "model_name": ..., "conf": ...}

    Returns:
        PDF 文件字节
    """
    meta = meta or {}
    st = _styles()
    buf = io.BytesIO()

    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title="水下目标检测报告", author="基于 GSCNet 的海洋检测平台",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=_page_footer)])

    story: List[Any] = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok_items = [it for it in items if not it.get("error")]

    # ---------- 封面区 ----------
    story.append(Paragraph("水下目标检测报告", st["title"]))
    story.append(Paragraph(
        f"基于 GSCNet 的海洋检测平台 · 生成时间 {now}", st["subtitle"]))

    total_objects = sum(int(it.get("total") or 0) for it in ok_items)
    avg_conf_all = None
    all_confs = [float(d.get("confidence") or 0)
                 for it in ok_items for d in (it.get("detections") or [])]
    if all_confs:
        avg_conf_all = sum(all_confs) / len(all_confs)

    overview_rows = [
        ("影像数量", f"{len(items)} 张 (成功 {len(ok_items)} / 失败 {len(items) - len(ok_items)})"),
        ("检出目标总数", f"{total_objects} 个"),
        ("识别类别数", str(len({c['class_name'] for it in ok_items for c in (it.get('counts') or [])}))),
        ("平均置信度", f"{avg_conf_all:.3f}" if avg_conf_all is not None else "-"),
        ("置信度阈值", str(meta.get("conf") or settings.conf_threshold)),
        ("模型", str(meta.get("model_name") or (settings.model_path_abs.name if settings.model_path_abs else "-"))),
        ("推理设备", settings.device_resolved),
    ]
    story.append(Paragraph("报告概览", st["h2"]))
    story.append(_kv_table([(k, v) for k, v in overview_rows], st,
                           widths=[42 * mm, 100 * mm]))
    story.append(Spacer(1, 8))

    # ---------- 批次总体结论 ----------
    story.append(Paragraph("批次总体结论", st["h2"]))
    story.append(_conclusion_box(_batch_conclusion(items), st))

    # ---------- 逐图分析 ----------
    for idx, item in enumerate(items, start=1):
        story.append(PageBreak())
        name = item.get("source_name") or f"影像 {idx}"
        title_flow = Paragraph(f"影像 {idx} / {len(items)} — {name}", st["h2"])
        story.append(title_flow)

        if item.get("error"):
            story.append(Paragraph(f"检测失败: {item['error']}", st["body"]))
            continue

        info_rows = [
            ("检出目标", f"{int(item.get('total') or 0)} 个"),
            ("图像分辨率", f"{item.get('width') or '-'} × {item.get('height') or '-'}"),
            ("推理耗时", f"{item.get('inference_ms') or '-'} ms"),
            ("置信度阈值", str(item.get("conf_threshold") or meta.get("conf") or "-")),
        ]
        counts = item.get("counts") or []
        dets = item.get("detections") or []
        analysis = _analyse(item, avg_conf_all)

        # 图片高度上限: 单图放宽到 84mm, 多图收紧到 62mm, 给统计块留空间
        img_max_h = 84 * mm if len(items) == 1 else 62 * mm
        _, img_h = _measure_image(item, _FRAME_W, img_max_h)

        # ---- 实测法: 先构建除明细表外的全部 flowable, 量出真实累计高度 ----
        info_tbl = _kv_table(info_rows, st, widths=[30 * mm, 60 * mm])
        class_tbl = _class_table(counts, int(item.get("total") or 0), st) if counts else None
        concl_box = _conclusion_box(analysis, st)
        img_flow = _load_image_flowable(item, _FRAME_W, st, max_h=img_h)

        det_tbl_title = Paragraph("检测明细", st["h3"])
        det_title_h = _flowable_h(det_tbl_title)

        def _upper_height(_img_h: float, _img_flow) -> float:
            h = 0.0
            h += _flowable_h(title_flow)
            h += _flowable_h(info_tbl)
            h += 4 * mm                                  # Spacer
            h += _img_h if _img_flow is not None else 0.0
            h += 5 * mm
            if class_tbl is not None:
                h += _flowable_h(Paragraph("类别统计", st["h3"]))
                h += _flowable_h(class_tbl)
                h += 4 * mm
            h += _flowable_h(Paragraph("结果分析", st["h3"]))
            h += _flowable_h(concl_box)
            h += 4 * mm + det_title_h
            return h

        rows_fit = _detail_rows_that_fit(_upper_height(img_h, img_flow), dets, st, det_title_h)

        # 若明细放不下且图片还有压缩余地, 逐步让出高度换行数
        if dets and rows_fit < 3 and img_flow is not None and img_h > 42 * mm:
            for shrink in (10 * mm, 18 * mm, 26 * mm):
                cand_h = max(42 * mm, img_h - shrink)
                if cand_h >= img_h:
                    continue
                cand_flow = _load_image_flowable(item, _FRAME_W, st, max_h=cand_h)
                rows_try = _detail_rows_that_fit(_upper_height(cand_h, cand_flow),
                                                 dets, st, det_title_h)
                if rows_try >= 3:
                    img_h, img_flow, rows_fit = cand_h, cand_flow, rows_try
                    break
                if cand_h <= 42 * mm:
                    break

        # ---- 正式写入 story ----
        story.append(info_tbl)
        story.append(Spacer(1, 4))
        if img_flow is not None:
            story.append(img_flow)
        else:
            story.append(Paragraph("(原图不可用, 仅输出统计数据)", st["small"]))
        story.append(Spacer(1, 5))

        if class_tbl is not None:
            story.append(Paragraph("类别统计", st["h3"]))
            story.append(class_tbl)
            story.append(Spacer(1, 4))

        story.append(Paragraph("结果分析", st["h3"]))
        story.append(concl_box)

        # 明细表: 行数由实测剩余空间决定, 确保整页不溢出
        if dets and rows_fit > 0:
            story.append(Spacer(1, 4))
            shown = min(len(dets), rows_fit)
            title = (f"检测明细 (共 {len(dets)} 条"
                     + (f", 显示前 {shown} 条)" if len(dets) > shown else ")"))
            story.append(Paragraph(title, st["h3"]))
            story.append(_detail_table(dets, st, limit=rows_fit))
        elif dets:
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                f"检测明细 (共 {len(dets)} 条, 因版面所限未展开, 完整框选请查看结果图):",
                st["h3"]))

    # ---------- 汇总页 ----------
    if len(items) > 1:
        story.append(PageBreak())
        story.append(Paragraph("全批次目标统计汇总", st["h2"]))
        agg: Dict[str, int] = {}
        for it in ok_items:
            for c in it.get("counts") or []:
                agg[c["class_name"]] = agg.get(c["class_name"], 0) + int(c["count"])
        if agg:
            story.append(_class_table(
                [{"class_name": k, "count": v} for k, v in agg.items()],
                total_objects, st))
        story.append(Spacer(1, 8))
        story.append(Paragraph("逐图产出对照", st["h3"]))
        rows = [["影像名称", "检出目标数", "主要类别"]]
        for it in ok_items:
            cs = sorted(it.get("counts") or [], key=lambda x: -x["count"])
            rows.append([
                Paragraph(str(it.get("source_name") or "-"), st["small"]),
                Paragraph(str(int(it.get("total") or 0)), st["small"]),
                Paragraph(cs[0]["class_name"] if cs else "无检出", st["small"]),
            ])
        t = Table(rows, colWidths=[86 * mm, 30 * mm, 34 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _C_HEAD_BG),
            ("GRID", (0, 0), (-1, -1), 0.4, _C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _C_ZEBRA]),
        ]))
        story.append(t)

    doc.build(story)
    return buf.getvalue()
