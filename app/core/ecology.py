"""生态多样性指标计算(纯统计, 不含任何模型 / 算法改动)。

职责边界
--------
输入是检测框的类别计数，输出是类别分布指数。模型类别不等于实际物种，
检测框也不等于经调查确认的独立生物个体。不能据此评价生态质量。

指标与公式
----------
记 S = 检出类别数, N = 检出框总数, p_i = n_i / N 为第 i 类占比。

1. **Shannon-Wiener 多样性指数**  H' = -Σ p_i · ln(p_i)
   综合"种类多少"与"分布是否均匀", 生态学最常用的多样性度量。H' 越大越多样。
2. **Simpson 优势度指数**        D  = 1 - Σ p_i²
   随机取两个个体属于不同类别的概率。D 越大越多样, 对优势种敏感。
3. **Pielou 均匀度指数**          J' = H' / ln(S)
   在"已知有 S 个类别"的前提下, 个体分布有多均匀。取值 0~1。
   S = 1 时无定义(ln 1 = 0), 返回 None 而非 0 —— 只有一类时"均匀度"没有意义。
4. **Margalef 丰富度指数**        d  = (S - 1) / ln(N)
   仅反映"类别丰富程度", 与个体总数做了归一。
5. **优势种 / 优势度**            占比最高的类别及其占比。

边界处理
--------
- 类别为空 / 总数为 0: 所有指数返回 None, 前端显示「—」, 不伪造 0。
- 仅 1 个类别: J' 返回 None(数学上无定义), H' = D = 0 是**正确**结果。
- 占比在计算前归一化, 避免上游 ratio 舍入误差污染指数。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


def _safe_log(x: float) -> float:
    return math.log(x) if x > 0 else 0.0


def compute_indicators(counts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """按类别计数计算生态多样性指标。

    Args:
        counts: `[{"class_name": "海胆", "count": 11}, ...]`, count 为非负整数。
                多余的字段(如 ratio)会被忽略, 计数取自 count。

    Returns:
        {
          "total": 检出框总数, "richness": 检出类别数,
          "shannon": H', "simpson": D, "pielou": J', "margalef": d,
          "evenness_note": 均匀度说明(S=1 时为无定义提示),
          "dominant": {"class_name", "count", "ratio"} | None,
          "abundance": [{"class_name", "count", "ratio", "cumulative_ratio"}...],
        }
    """
    # 过滤非法计数: count <= 0 的类别不参与(它们不是"检出到的物种")
    by_name: Dict[str, int] = {}
    for item in counts or []:
        try:
            n = int(item.get("count") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if n <= 0:
            continue
        name = str(item.get("class_name") or "未知")
        by_name[name] = by_name.get(name, 0) + n

    cleaned = [{"class_name": name, "count": n} for name, n in by_name.items()]

    cleaned.sort(key=lambda x: x["count"], reverse=True)
    total = sum(c["count"] for c in cleaned)
    richness = len(cleaned)

    empty = {
        "total": 0, "richness": 0,
        "shannon": None, "simpson": None, "pielou": None, "margalef": None,
        "evenness_note": "暂无检出数据",
        "dominant": None, "abundance": [], "shannon_max": None,
    }
    if total <= 0 or richness == 0:
        return empty

    # ---- 归一化占比(以此为准, 不信任上游 ratio 字段) ----
    props = [c["count"] / total for c in cleaned]

    shannon = -sum(p * _safe_log(p) for p in props)
    simpson = 1.0 - sum(p * p for p in props)
    # 单类别时 p=1, ln(1)=0, 求和得 0 但可能因 IEEE754 得到 -0.0;
    # 加 0.0 归一化负零, 避免前端显示 "-0"
    shannon += 0.0
    simpson += 0.0

    # Pielou 均匀度: S = 1 时 ln(S) = 0, 数学上无定义
    if richness > 1:
        max_shannon = math.log(richness)
        pielou: Optional[float] = shannon / max_shannon if max_shannon > 0 else None
        evenness_note = "取值 0~1，越接近 1 表示已检出类别的框数分布越均匀"
    else:
        pielou = None
        evenness_note = "仅检出 1 个类别, 均匀度无定义(需 ≥2 个类别)"

    # Margalef 丰富度: 依赖 ln(N), N = 1 时 ln(1) = 0 导致除零
    margalef: Optional[float] = None
    if total > 1:
        ln_n = _safe_log(float(total))
        margalef = (richness - 1) / ln_n if ln_n > 0 else None

    # ---- 丰度表(含累计占比, 供帕累托图使用) ----
    abundance: List[Dict[str, Any]] = []
    cumulative = 0.0
    for c, p in zip(cleaned, props):
        cumulative += p
        abundance.append({
            "class_name": c["class_name"],
            "count": c["count"],
            "ratio": round(p * 100, 2),
            "cumulative_ratio": round(cumulative * 100, 2),
        })

    dominant = dict(abundance[0]) if abundance else None

    return {
        "total": total,
        "richness": richness,
        "shannon": round(shannon, 4),
        "simpson": round(simpson, 4),
        "pielou": round(pielou, 4) if pielou is not None else None,
        "margalef": round(margalef, 4) if margalef is not None else None,
        "shannon_max": round(math.log(richness), 4) if richness > 1 else 0.0,
        "evenness_note": evenness_note,
        "dominant": dominant,
        "abundance": abundance,
    }


def interpret(result: Dict[str, Any]) -> Dict[str, Any]:
    """描述检出类别分布，不把模型类别或固定阈值当作生态评价依据。"""
    h = result.get("shannon")
    j = result.get("pielou")
    d = result.get("dominant")
    richness = result.get("richness") or 0
    total = result.get("total") or 0

    if not total:
        return {"level": "none", "shannon_text": "暂无数据", "evenness_text": "暂无数据",
                "dominant_text": "暂无数据"}

    if h is None:
        shannon_text = "不可用"
    elif richness < 2:
        shannon_text = "仅检出一个类别，不评价实际物种多样性"
    else:
        shannon_text = f"当前 {richness} 个检出类别的理论上限为 ln({richness}) = {math.log(richness):.4f}"

    evenness_text = ("仅一个检出类别，均匀度无定义" if j is None else
                     f"达到当前类别数下最大 Shannon 值的 {j * 100:.1f}%；仅反映检出框分布")

    dominant_text = "暂无数据"
    if d:
        dominant_text = f"检出最多的类别为「{d['class_name']}」，占 {d['ratio']}%"

    return {
        "level": "descriptive",
        "shannon_text": shannon_text,
        "evenness_text": evenness_text,
        "dominant_text": dominant_text,
    }


def build_payload(counts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """计算指标 + 生成解读, 供接口直接返回。"""
    result = compute_indicators(counts)
    result["interpretation"] = interpret(result)
    return result
