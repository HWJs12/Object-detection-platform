"""调查维度(站点 / 样方 / 水深 / 调查轮次)字段的清洗与规整。

独立成模块的原因: 图片检测链路(app.core.detection_service)与视频检测链路
(app.core.video_task)都要写这几个字段。若把函数放在 video_task 里再被
detection_service 引用, 就会形成 detection_service -> video_task 的跨模块依赖,
而 video_task 本身依赖 yolo_engine / dao, 后续极易演化为循环导入。

这里只依赖标准库, 任何层都可以安全导入。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

# 数据库列长度上限(与 schema.sql 保持一致), 超长直接截断
SURVEY_TEXT_LIMITS = (("site", 64), ("transect", 64), ("survey_round", 32))

# 未标注时的展示文案, 读层统一使用, 避免前端各处硬编码不同说法
SITE_UNLABELED = "未标注站点"
TRANSECT_UNLABELED = "未标注样方"
ROUND_UNLABELED = "未标注轮次"


def normalize_survey(survey: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """清洗站点 / 样方等调查维度字段。

    统一在此处收口的原因: 图片与视频两条链路都要写这几个字段, 若各自清洗,
    会出现"图片记录去空格、视频记录不去"的口径分裂。规则:
      - 文本字段去首尾空白, 超长截断(库字段不是无限长, 也不该容纳整段描述);
      - 水深必须是有限的正数, 否则视为未提供(None), 而不是存 0 让统计被拉低;
      - 空串一律存为空串, 由读层统一翻译成"未标注", 不在这里造标签。
    """
    survey = survey or {}
    result: Dict[str, Any] = {}
    for key, limit in SURVEY_TEXT_LIMITS:
        raw = survey.get(key)
        text = "" if raw is None else str(raw).strip()
        result[key] = text[:limit]
    depth = survey.get("depth_m")
    depth_value: Optional[float] = None
    if depth not in (None, ""):
        try:
            candidate = float(depth)
            if math.isfinite(candidate) and candidate > 0:
                depth_value = round(candidate, 2)
        except (TypeError, ValueError):
            depth_value = None
    if "site_id" in survey:
        result["site_id"] = survey["site_id"]
    result["depth_m"] = depth_value
    return result


def survey_from_form(getter) -> Dict[str, Any]:
    """从表单/查询参数中提取调查维度字段。

    getter 为任一取值函数(如 FastAPI 的 Form 参数对象或 dict.get),
    这里只负责挑字段并交给 normalize_survey, 便于图片、批量、视频三个接口复用。
    """
    return normalize_survey({
        "site": getter("site"),
        "transect": getter("transect"),
        "depth_m": getter("depth_m"),
        "survey_round": getter("survey_round"),
    })
