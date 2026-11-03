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
    Flowable,
    Image,
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
        lines.append("本图未检出达到当前阈值的训练类别目标。未检出不代表实际不存在目标, "
                     "需结合原图和人工标注检查漏检。")
        return lines

    # 1) 只陈述画面检出框数; 未经面积标定不能推断实际生物密度。
    width = item.get("width") or 0
    height = item.get("height") or 0
    lines.append(f"本图共检出 {total} 个目标框; 未提供实际视野面积, 不计算生物密度。")

    # 2) 类别构成
    if counts:
        top = sorted(counts, key=lambda c: -c["count"])
        head, *rest = top
        share = head["count"] / total * 100 if total else 0
        desc = f"以「{head['class_name']}」为主, 计 {head['count']} 个, 占 {share:.0f}%"
        if rest:
            others = ", ".join(f"{c['class_name']} {c['count']} 个" for c in rest[:4])
            desc += f"; 其余为 {others}"
        lines.append(desc + f"。共检出 {len(top)} 个模型类别, 仅描述本图的模型输出分布。")

    # 3) 置信度水平
    if dets:
        confs = [float(d.get("confidence") or 0) for d in dets]
        avg = sum(confs) / len(confs)
        high = sum(1 for c in confs if c >= 0.7)
        low = sum(1 for c in confs if c < 0.4)
        lines.append(f"模型输出置信度均值 {avg:.3f}; "
                     f"其中 {high} 个目标的置信度达到 0.70 以上, "
                     f"{low} 个低于 0.40。置信度不等于经验证的准确率。")
        if low:
            lines.append(f"存在 {low} 个低置信度目标, 建议结合原始影像人工复核, 不据此推断成像原因。")

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
            lines.append("本图模型输出置信度高于本批次平均值, 仍需人工核对。")
        elif batch_avg_conf - item_avg > 0.08:
            lines.append("本图模型输出置信度低于本批次平均值, 建议优先复核。")

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
        lines.append(f"全批次模型输出平均置信度 {avg:.3f}。该数值不代表准确率, "
                     "正式统计前需使用人工真值验证并复核误检与漏检。")

    # 相似图提示(同源图片重复上传)
    names = [it.get("source_hash") or it.get("saved_name") or it.get("source_name") or "" for it in ok]
    if len(names) != len(set(names)):
        dupes = {n for n in names if names.count(n) > 1}
        lines.append(f"注意: 本批含 {len(dupes)} 组重复素材, 可能导致同一目标被重复统计, "
                     f"汇总数据解读时需留意。")

    if failed:
        lines.append(f"另有 {len(failed)} 张影像检测失败: "
                     + "; ".join(f"{it.get('source_name') or '-'}({it.get('error')})"
                                 for it in failed[:3])
                     + ("..." if len(failed) > 3 else "") + "。")

    lines.append(f"结论: 本次检测覆盖 {len(ok)} 张水下影像, 共识别 {total_objects} 个目标, "
                 f"涉及 {len(agg)} 个类别, 检测流程完整执行。")
    return lines


def _provenance_labels(items: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """只使用检测时保存的信息; 历史缺失信息不能用当前设置补造。"""
    models, devices = set(), set()
    for item in items:
        snapshot = item.get("inference_snapshot") or {}
        model = snapshot.get("model") or {}
        name = model.get("name") or "历史记录未保存"
        if model.get("sha256"):
            name += f" [{model['sha256'][:12]}]"
        models.add(name)
        devices.add(snapshot.get("device") or "历史记录未保存")
    return {"model": "; ".join(sorted(models)) or "未保存", "device": "; ".join(sorted(devices)) or "未保存"}


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


class _RatioBar(Flowable):
    """用矢量矩形绘制占比, 避免中文字体缺失方块字符。"""
    def __init__(self, percentage: float):
        super().__init__()
        self.width = 52 * mm
        self.height = 4 * mm
        self.ratio = max(0.0, min(1.0, percentage / 100))

    def draw(self):
        self.canv.setFillColor(_C_HEAD_BG)
        self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        self.canv.setFillColor(_C_PRIMARY)
        self.canv.rect(0, 0, self.width * self.ratio, self.height, fill=1, stroke=0)


def _class_table(counts: Sequence[Dict[str, Any]], total: int, st) -> Table:
    header = ["类别", "数量", "占比", "占比示意"]
    data = [[Paragraph(f"<b>{h}</b>", st["small"] if False else st["body"]) for h in header]]
    for c in sorted(counts, key=lambda x: -x["count"]):
        pct = c["count"] / total * 100 if total else 0
        data.append([
            Paragraph(str(c["class_name"]), st["body"]),
            Paragraph(str(c["count"]), st["body"]),
            Paragraph(f"{pct:.1f}%", st["body"]),
            _RatioBar(pct),
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
    canvas.drawString(20 * mm, 12 * mm, "基于 GSCNet 的海洋检测平台 · 检测报告")
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

    provenance = _provenance_labels(items)
    thresholds = sorted({str(it.get("conf_threshold")) for it in items if it.get("conf_threshold") is not None})

    overview_rows = [
        ("影像数量", f"{len(items)} 张 (成功 {len(ok_items)} / 失败 {len(items) - len(ok_items)})"),
        ("检出目标总数", f"{total_objects} 个"),
        ("识别类别数", str(len({c['class_name'] for it in ok_items for c in (it.get('counts') or [])}))),
        ("平均置信度", f"{avg_conf_all:.3f}" if avg_conf_all is not None else "-"),
        ("置信度阈值", ", ".join(thresholds) or "历史记录未保存"),
        ("检测时模型", provenance["model"]),
        ("检测时设备", provenance["device"]),
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
            ("置信度阈值", str(item["conf_threshold"]) if item.get("conf_threshold") is not None else "历史记录未保存"),
            ("模型版本", _provenance_labels([item])["model"]),
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

        def _upper_height(_img_h: float, _img_flow, *, title_flow=title_flow,
                          info_tbl=info_tbl, class_tbl=class_tbl,
                          concl_box=concl_box, det_title_h=det_title_h) -> float:
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

    # ---------- 方法学附录 ----------
    # 放在汇总页之后: 读者先看到结果, 再按需核对口径。附录内容全部来自本批记录
    # 实际携带的元信息, 缺什么就写明"未记录", 不用默认值填补。
    story.append(PageBreak())
    _append_methodology(story, items, ok_items, meta, st)

    doc.build(story)
    return buf.getvalue()


def _append_methodology(story: List[Any], items: Sequence[Dict[str, Any]],
                        ok_items: Sequence[Dict[str, Any]],
                        meta: Dict[str, Any], st) -> None:
    """报告方法学附录: 数据来源 / 计数口径 / 指标定义 / 适用边界 / 复现要素。

    这一段的作用不是再念一遍结论, 而是让审阅者能判断"这个数字在什么前提下成立"。
    因此每条都给出定义式与已知局限, 而不是只给名词。
    """
    story.append(Paragraph("附录 A · 方法学说明", st["h2"]))
    story.append(Paragraph(
        "本附录记录本次报告的数据来源、计数口径与指标定义，供结果复核与方法引用。"
        "其中“未记录”表示该要素在检测时未写入数据库，系统不会用默认值补推。",
        st["body"]))
    story.append(Spacer(1, 6))

    # ---- A.1 数据来源与可复现要素 ----
    story.append(Paragraph("A.1 数据来源与复现要素", st["h3"]))
    provenance = _provenance_labels(items)
    batch_id = meta.get("batch_id") or "-"
    created = sorted({str(it.get("created_at")) for it in ok_items if it.get("created_at")})
    hashes = {str(it.get("source_hash")) for it in ok_items if it.get("source_hash")}
    # 推理参数取自保存时的快照; 不同记录可能不同, 因此逐项去重后并列展示
    params: set = set()
    for it in ok_items:
        snapshot = it.get("inference_snapshot") or {}
        for key, value in (snapshot.get("parameters") or {}).items():
            params.add(f"{key}={value}")
    story.append(_kv_table([
        ("批次标识", str(batch_id)),
        ("影像数量", f"{len(items)} 张（成功 {len(ok_items)}，失败 {len(items) - len(ok_items)}）"),
        ("检测时间", ", ".join(created[:3]) + (" 等" if len(created) > 3 else "") if created else "-"),
        ("推理模型", provenance["model"]),
        ("推理设备", provenance["device"]),
        ("推理参数", ", ".join(sorted(params)) if params else "未记录"),
        ("源文件哈希", (f"{len(hashes)} 个唯一 SHA-256" if hashes
                        else "未记录（历史记录无内容哈希，无法据哈希复现同一文件）")),
    ], st, widths=[34 * mm, 130 * mm]))
    story.append(Spacer(1, 8))

    # ---- A.2 调查维度 ----
    story.append(Paragraph("A.2 调查维度", st["h3"]))
    sites = sorted({str(it.get("site")) for it in ok_items if it.get("site")})
    transects = sorted({str(it.get("transect")) for it in ok_items if it.get("transect")})
    depths = sorted({float(it["depth_m"]) for it in ok_items
                     if it.get("depth_m") not in (None, "")})
    rounds = sorted({str(it.get("survey_round")) for it in ok_items if it.get("survey_round")})
    unlabeled = sum(1 for it in ok_items if not it.get("site"))
    story.append(_kv_table([
        ("站点", ", ".join(sites) if sites else "未标注"),
        ("样方 / 断面", ", ".join(transects) if transects else "未标注"),
        ("水深 (m)", ", ".join(f"{d:g}" for d in depths) if depths else "未记录"),
        ("调查轮次", ", ".join(rounds) if rounds else "未标注"),
        ("未标注站点的影像", f"{unlabeled} 张"
                             + ("（这部分不计入站点间比较）" if unlabeled else "")),
    ], st, widths=[34 * mm, 130 * mm]))
    story.append(Spacer(1, 8))

    # ---- A.3 计数口径 ----
    story.append(Paragraph("A.3 计数口径", st["h3"]))
    story.append(Paragraph(
        "同一批影像的数字必须可归因到具体的计数口径，否则不同报告之间无法比较。"
        "本系统按下列顺序取用，并在每条记录上保存实际采用的那一档：",
        st["body"]))
    calibers = [
        ("图片检出框", "单张影像经模型输出的边界框数量，一框即一个检出目标。",
         "不涉及跨帧去重，因此不存在“重复计数”问题，但也不代表独立生物个体数。"),
        ("跨帧轨迹数", "在连续帧上关联同一目标后形成的轨迹条数，用于视频。",
         "依赖跨帧关联成立；目标出画后重新进入会生成新轨迹，轨迹数可能偏多。"),
        ("单帧峰值 MaxN", "同一帧内某类别同时出现的最大数量，取全片各帧的最大值。",
         "只要求帧内不重复计数，不依赖跨帧识别；通常低估实际经过的目标总数。"),
        ("CPUnx", "MaxN ÷ 调查时长（小时），即单位努力量的相对丰度。",
         "用于不同时长素材之间的相对比较；时长缺失时不计算，不以 0 代替。"),
    ]
    # _kv_table 是两列结构, 这里拆成"定义表 + 局限说明"两段, 避免把局限挤进单元格
    story.append(_kv_table([(name, definition) for name, definition, _ in calibers],
                           st, widths=[30 * mm, 134 * mm]))
    for name, _definition, limit_note in calibers:
        story.append(Paragraph(f"<b>{name} 的局限</b>：{limit_note}", st["small"]))

    # 本批实际采用的口径分布: 让读者知道这份报告的数字落在哪一档
    mode_label = {
        "image_objects": "图片检出框", "tracked_objects": "跨帧轨迹数",
        "tracking_partial": "单帧峰值 MaxN", "frame_detections": "累计检出框",
        "legacy_unknown": "历史记录(口径未保存)",
    }
    mode_count: Dict[str, int] = {}
    for it in ok_items:
        key = mode_label.get(str(it.get("counting_mode")), str(it.get("counting_mode") or "未知"))
        mode_count[key] = mode_count.get(key, 0) + 1
    if mode_count:
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "本批实际采用的口径：" + "；".join(f"{k} {v} 张" for k, v in sorted(mode_count.items())),
            st["body"]))
    maxn_items = [it for it in ok_items if int(it.get("maxn_total") or 0) > 0]
    if maxn_items:
        story.append(Paragraph(
            f"其中 {len(maxn_items)} 张给出了单帧峰值 MaxN（最高 {max(it['maxn_total'] for it in maxn_items)} 条）；"
            "MaxN 为相对丰度，不等同于生物个体数。", st["small"]))
    story.append(Spacer(1, 6))

    # ---- A.4 指标定义 ----
    story.append(Paragraph("A.4 指标定义", st["h3"]))
    # 注意: 不使用 Unicode 下标字符(i 的位置用普通 i), 中文字体缺少这些字形,
    # 导出 PDF 时会渲染成空白, 公式会被读成 "p ·ln p"。
    story.append(_kv_table([
        ("Shannon H'", "H' = -Σ p_i·ln(p_i)，p_i 为类别 i 的检出框数占比, i = 1..S"),
        ("Simpson D", "D = 1 - Σ p_i^2"),
        ("Pielou J'", "J' = H' / ln(S)，S 为检出类别数；S ≤ 1 时不可计算"),
        ("Margalef d", "d = (S - 1) / ln(N)，N 为检出框数；N ≤ 1 时不可计算"),
        ("平均置信度", "Σ confidence / 检出框数，仅描述模型输出，不是准确率"),
    ], st, widths=[34 * mm, 130 * mm]))
    story.append(Spacer(1, 8))

    # ---- A.5 适用边界 ----
    story.append(Paragraph("A.5 适用边界与已知局限", st["h3"]))
    boundaries = [
        "本报告的类别与计数来自模型推理结果，未经人工逐框核验；置信度是模型自评，不等同于识别准确率。",
        "检出类别数不等于实际物种数：模型类别体系之外的生物不会被识别，稀有物种也可能漏检。",
        "检出框数不等于个体总量：图片之间可能拍到同一目标，视频中同一目标可能被多次检出。",
        "相对丰度指标适用于同一采集条件下的比较；跨设备、跨站点、跨季节的绝对值不具可比性。",
        "同一素材的重复检测在批次统计中会重复累加，需要在站点维度或去重口径下另行统计。",
        "报告不对生态质量、种群健康或保护等级作出判断，相关结论需结合人工调查与统计检验。",
    ]
    story.append(_conclusion_box(boundaries, st))

