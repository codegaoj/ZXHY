"""Agent 辅助审核模块（增强版 v2）。

每日候选股票生成后，自动审核候选股的异常情况，给出「建议关注/谨慎/排除」意见。

审核维度（13项）：
    1. 标签矛盾检测
    2. 评分合理性
    3. 板块集中度
    4. 涨幅分布
    5. 量价异常
    6. 技术指标一致性（MACD方向、均线偏离）
    7. 图形质量与评分对齐
    8. 板块弱势高分检测
    9. 支撑压力位风险
    10. 多信号共振/亮点识别
    11. 高位风险检测（距支撑位距离、涨幅位置）
    12. 趋势完整性检测（白线/黄线排列、MACD方向一致性）
    13. 流动性风险检测（成交额、量比综合判断）

数据来源：outputs/daily_candidates.csv 的行字典。
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class CandidateReview:
    symbol: str
    name: str
    opinion: str  # "建议关注" / "谨慎" / "排除"
    issues: List[str]  # 发现的问题列表
    highlights: List[str]  # 亮点列表
    score: float
    risk_reward: float  # 风险收益比（>0有效，0表示无法计算）
    risk_level: int  # 风险等级 0-5（0=无风险，5=极高风险）
    position_suggestion: str  # 仓位建议文本
    brief: str  # 一句话综合评价


@dataclass
class BatchReview:
    reviews: List[CandidateReview]
    summary: str
    warnings: List[str]
    recommendations: List[str]  # 操作建议
    stats: dict


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text in {"", "-", "nan", "None", "null"}:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def _parse_tags(tags_field) -> set:
    if tags_field is None:
        return set()
    if isinstance(tags_field, (list, tuple, set)):
        return {str(t).strip() for t in tags_field if str(t).strip()}
    raw = str(tags_field)
    normalized = raw.replace(";", "；").replace(",", "；").replace("，", "；")
    return {part.strip() for part in normalized.split("；") if part.strip()}


def _compute_ranks(candidates: list[dict]) -> dict:
    scored: list[tuple[float, int]] = []
    for idx, row in enumerate(candidates):
        scored.append((_to_float(row.get("candidate_score")), idx))
    scored.sort(key=lambda item: (-item[0], item[1]))
    rank_map: dict[int, int] = {}
    for rank, (_, idx) in enumerate(scored, start=1):
        rank_map[idx] = rank
    return rank_map


# ---------------------------------------------------------------------------
# 维度 1：标签矛盾检测
# ---------------------------------------------------------------------------


def _detect_tag_conflicts(tags: set, pct_change: float) -> list[str]:
    issues: list[str] = []

    if "突破压力" in tags and "破白线" in tags:
        issues.append("[矛盾]突破压力与破白线共存")

    if "滴滴战法" in tags and pct_change > 8:
        issues.append(f"[矛盾]滴滴战法但涨幅{pct_change:.1f}%>8%")

    if "锤头线" in tags and "放量长阳" in tags:
        issues.append("[矛盾]锤头线与放量长阳形态冲突")

    if "B1/B2买点" in tags and "双线/白黄线" not in tags:
        issues.append("[矛盾]B1/B2买点但知行趋势不达标")

    if "单针战法" in tags and pct_change > 5:
        issues.append(f"[可疑]单针战法但涨幅{pct_change:.1f}%>5%")

    if "回踩白线" in tags and "破白线" in tags:
        issues.append("[矛盾]回踩白线与破白线共存")

    return issues


# ---------------------------------------------------------------------------
# 维度 2：评分合理性
# ---------------------------------------------------------------------------


def _check_score_rationality(row: dict, tags: set, rank: int) -> list[str]:
    issues: list[str] = []
    score = _to_float(row.get("candidate_score"))
    risk = str(row.get("risk", "")).strip()
    pct_change = _to_float(row.get("pct_change"))

    if score > 120 and risk not in ("无", ""):
        issues.append(f"评分偏高但有风险：{score:.0f}分，风险={risk}")

    if score < 60 and rank <= 10:
        issues.append(f"评分偏低但排名靠前：{score:.0f}分，排名{rank}")

    if len(tags) >= 6:
        issues.append(f"标签过多（{len(tags)}个），可能过度拟合")
    elif len(tags) >= 5:
        issues.append(f"[轻微]标签偏多（{len(tags)}个）")

    if pct_change > 7 and score > 110:
        issues.append(f"追高风险：涨幅{pct_change:.1f}%且评分{score:.0f}，建议回调后关注")

    return issues


# ---------------------------------------------------------------------------
# 维度 5：量价异常
# ---------------------------------------------------------------------------


def _check_volume_price(volume_ratio: float, pct_change: float) -> list[str]:
    issues: list[str] = []

    if volume_ratio > 3.0:
        issues.append(f"异常放量：量比{volume_ratio:.1f}>3.0，可能主力出货")
    elif volume_ratio > 2.5:
        issues.append(f"[轻微]量比偏高：{volume_ratio:.1f}，需关注持续性")

    if volume_ratio < 0.5:
        issues.append(f"缩量明显：量比{volume_ratio:.1f}<0.5，流动性不足")

    if pct_change > 5 and volume_ratio < 1.0:
        issues.append(f"量价背离：涨幅{pct_change:.1f}%但量比{volume_ratio:.1f}<1.0")

    if pct_change < -3 and volume_ratio > 2.0:
        issues.append(f"放量下跌：跌幅{pct_change:.1f}%且量比{volume_ratio:.1f}，恐慌出逃")

    return issues


# ---------------------------------------------------------------------------
# 维度 6：技术指标一致性
# ---------------------------------------------------------------------------


def _check_technical_consistency(row: dict, tags: set) -> list[str]:
    """检查技术指标之间的逻辑一致性。"""
    issues: list[str] = []
    macd_dif = _to_float(row.get("macd_dif"))
    macd_dea = _to_float(row.get("macd_dea"))
    close = _to_float(row.get("close"))
    white_line = _to_float(row.get("white_line"))
    yellow_line = _to_float(row.get("yellow_line"))

    # MACD方向检查
    if macd_dif < macd_dea and macd_dif < 0 and "MACD 多头" in tags:
        issues.append("[矛盾]标签MACD多头但DIF<DEA且DIF<0")
    elif macd_dif < macd_dea and "MACD 多头" in tags:
        issues.append("[可疑]标签MACD多头但DIF<DEA（即将死叉）")

    # 均线偏离检查
    if white_line > 0 and close > 0:
        deviation_white = (close - white_line) / white_line * 100
        if deviation_white > 15:
            issues.append(f"偏离白线过大：收盘价高于白线{deviation_white:.1f}%，回调风险")

    if yellow_line > 0 and close > 0:
        deviation_yellow = (close - yellow_line) / yellow_line * 100
        if deviation_yellow > 20:
            issues.append(f"偏离黄线过大：收盘价高于黄线{deviation_yellow:.1f}%，中期超买")

    # 白线在黄线下方但标签有多头
    if white_line > 0 and yellow_line > 0 and white_line < yellow_line:
        if "双线/白黄线" in tags:
            issues.append("[可疑]白线低于黄线但标记为双线多头")

    return issues


# ---------------------------------------------------------------------------
# 维度 7：图形质量与评分对齐
# ---------------------------------------------------------------------------


def _check_chart_quality_alignment(row: dict) -> list[str]:
    """检查图形质量评分与候选评分是否一致。"""
    issues: list[str] = []
    chart_quality = int(_to_float(row.get("chart_quality"), 50))
    chart_grade = str(row.get("chart_grade", "")).strip()
    score = _to_float(row.get("candidate_score"))

    if chart_quality < 40 and score > 100:
        issues.append(f"图形质量差但评分高：图形{chart_quality}分，候选{score:.0f}分")

    if chart_quality < 50 and score > 120:
        issues.append(f"图形质量一般但评分很高：图形{chart_quality}分，候选{score:.0f}分")

    if chart_grade == "较差" and score > 90:
        issues.append(f"图形质量较差（{chart_quality}分）但评分{score:.0f}，形态风险")

    return issues


# ---------------------------------------------------------------------------
# 维度 8：板块弱势高分检测
# ---------------------------------------------------------------------------


def _check_sector_weakness(row: dict) -> list[str]:
    """检查板块弱势但个股高分的矛盾。"""
    issues: list[str] = []
    sector_strength = str(row.get("sector_strength", "")).strip()
    sector_change = _to_float(row.get("sector_change"))
    score = _to_float(row.get("candidate_score"))

    if sector_strength == "弱势" and score > 110:
        issues.append(
            f"板块弱势但个股高分：板块涨{sector_change:.1f}%，评分{score:.0f}，逆板块上涨需警惕"
        )

    if sector_strength == "弱势" and sector_change < -1.5:
        issues.append(f"板块明显下跌：板块涨{sector_change:.1f}%，个股逆势可能难持续")

    return issues


# ---------------------------------------------------------------------------
# 维度 9：支撑压力位风险与风险收益比
# ---------------------------------------------------------------------------


def _check_support_resistance(row: dict) -> tuple[list[str], float]:
    """检查支撑压力位风险，返回（问题列表, 风险收益比）。"""
    issues: list[str] = []
    close = _to_float(row.get("close"))
    support = _to_float(row.get("support_price"))
    resistance = _to_float(row.get("resistance_price"))

    risk_reward = 0.0

    if close <= 0:
        return issues, 0.0

    # 距压力位过近
    if resistance > 0 and resistance > close:
        distance_up = (resistance - close) / close * 100
        if distance_up < 2:
            issues.append(f"逼近压力位：距压力位仅{distance_up:.1f}%，上行空间有限")
        elif distance_up < 5:
            issues.append(f"[轻微]距压力位{distance_up:.1f}%，空间偏小")
    elif resistance > 0 and resistance <= close:
        # 已突破压力位
        pass

    # 距支撑位过远（风险大）
    if support > 0 and support < close:
        distance_down = (close - support) / close * 100
        if distance_down > 15:
            issues.append(f"远离支撑位：距支撑位{distance_down:.1f}%，回调空间大")

    # 计算风险收益比 = 上行空间 / 下行风险
    if resistance > close and support < close and support > 0:
        upside = resistance - close
        downside = close - support
        if downside > 0:
            risk_reward = round(upside / downside, 2)
            if risk_reward < 1.0:
                issues.append(f"风险收益比偏低：{risk_reward}（上行{upside/close*100:.1f}% vs 下行{downside/close*100:.1f}%）")
            elif risk_reward >= 3.0:
                pass  # 好的风险收益比，不加issue

    return issues, risk_reward


# ---------------------------------------------------------------------------
# 维度 10：亮点识别
# ---------------------------------------------------------------------------


def _identify_highlights(row: dict, tags: set, risk_reward: float) -> list[str]:
    """识别候选股的正面亮点。"""
    highlights: list[str] = []

    # 多信号共振
    signal_tags = {"滴滴战法", "放量长阳", "锤头线", "回踩白线", "突破压力", "B1/B2买点", "单针战法"}
    signal_count = len(tags & signal_tags)
    if signal_count >= 3:
        highlights.append(f"多信号共振（{signal_count}个买点信号叠加）")
    elif signal_count >= 2:
        highlights.append(f"双信号共振（{signal_count}个买点信号）")

    # 板块+图形双强
    sector_strength = str(row.get("sector_strength", "")).strip()
    chart_grade = str(row.get("chart_grade", "")).strip()
    if sector_strength == "强势" and chart_grade == "优秀":
        highlights.append("板块+图形双强")

    # 好的风险收益比
    if risk_reward >= 2.0:
        highlights.append(f"风险收益比优秀（{risk_reward}）")

    # MACD+知行趋势+图形三好
    has_macd = "MACD 多头" in tags
    has_trend = "双线/白黄线" in tags
    if has_macd and has_trend and chart_grade == "优秀":
        highlights.append("MACD+知行趋势+图形三好")

    return highlights


# ---------------------------------------------------------------------------
# 维度 11：高位风险检测
# ---------------------------------------------------------------------------


def _check_position_risk(row: dict, pct_change: float) -> list[str]:
    """检测个股处于高位的风险。"""
    issues: list[str] = []
    close = _to_float(row.get("close"))
    support = _to_float(row.get("support_price"))
    resistance = _to_float(row.get("resistance_price"))

    if close <= 0:
        return issues

    # 距支撑位过远 = 高位
    if support > 0 and support < close:
        distance_down = (close - support) / close * 100
        if distance_down > 20:
            issues.append(f"高位风险：距支撑位{distance_down:.1f}%，回调空间大")
        elif distance_down > 12 and pct_change > 5:
            issues.append(f"[轻微]偏高位置+涨幅{pct_change:.1f}%，追高需谨慎")

    # 已突破压力位但在高位
    if resistance > 0 and close > resistance:
        above_resist = (close - resistance) / resistance * 100
        if above_resist > 5:
            issues.append(f"突破压力位后偏离{above_resist:.1f}%，获利盘压力大")

    return issues


# ---------------------------------------------------------------------------
# 维度 12：趋势完整性检测
# ---------------------------------------------------------------------------


def _check_trend_integrity(row: dict, tags: set) -> list[str]:
    """检测趋势排列的完整性。"""
    issues: list[str] = []
    white_line = _to_float(row.get("white_line"))
    yellow_line = _to_float(row.get("yellow_line"))
    macd_dif = _to_float(row.get("macd_dif"))
    macd_dea = _to_float(row.get("macd_dea"))

    # 白线/黄线空头排列（白线在黄线下方）
    if white_line > 0 and yellow_line > 0 and white_line < yellow_line:
        gap_pct = (yellow_line - white_line) / yellow_line * 100
        if gap_pct > 3:
            issues.append(f"趋势空头排列：白线低于黄线{gap_pct:.1f}%，中期趋势偏弱")

    # MACD死叉或即将死叉
    if macd_dif > 0 and macd_dea > 0 and macd_dif < macd_dea:
        diff = macd_dea - macd_dif
        if diff > 0 and macd_dif > 0:
            issues.append("[可疑]MACD即将死叉：DIF开始低于DEA，多头动能减弱")

    # 白线走平或下行但标记多头
    if "双线/白黄线" in tags and white_line > 0 and yellow_line > 0:
        if white_line < yellow_line * 0.98:
            issues.append("[可疑]标记双线多头但白线明显低于黄线，趋势不成立")

    return issues


# ---------------------------------------------------------------------------
# 维度 13：流动性风险检测
# ---------------------------------------------------------------------------


def _check_liquidity_risk(row: dict, volume_ratio: float) -> list[str]:
    """检测流动性风险。"""
    issues: list[str] = []
    amount = _to_float(row.get("amount"))

    # 成交额过小
    if 0 < amount < 80_000_000:
        issues.append(f"流动性不足：成交额{amount / 1e8:.2f}亿<0.8亿，大资金进出困难")

    # 量比异常低 + 成交额低
    if volume_ratio < 0.5 and amount < 150_000_000:
        issues.append(f"缩量低流动性：量比{volume_ratio:.1f}+成交额{amount / 1e8:.2f}亿，关注资金参与度")

    return issues


# ---------------------------------------------------------------------------
# 仓位建议生成
# ---------------------------------------------------------------------------


def _generate_position_suggestion(
    opinion: str,
    risk_level: int,
    risk_reward: float,
    pct_change: float,
    plan_position_pct: float,
) -> str:
    """根据审核结果生成仓位建议。"""
    if opinion == "排除":
        return "不建议买入"

    if opinion == "谨慎":
        if risk_level >= 3:
            return "轻仓试探（计划仓位的30%-50%）"
        return "半仓参与（计划仓位的50%-70%）"

    # 建议关注
    if risk_reward >= 3.0 and risk_level <= 1:
        return f"可按计划仓位执行（{plan_position_pct:.0f}%）"
    elif risk_reward >= 2.0 and risk_level <= 2:
        return f"适中仓位（计划仓位{plan_position_pct:.0f}%的70%-80%）"
    elif pct_change > 5:
        return "回调后再买入，初始仓位不超过计划的50%"
    else:
        return f"分批建仓（先{plan_position_pct * 0.5:.0f}%，确认趋势后加仓）"


# ---------------------------------------------------------------------------
# 简评生成
# ---------------------------------------------------------------------------


def _generate_brief(
    name: str,
    opinion: str,
    risk_level: int,
    risk_reward: float,
    highlights: list[str],
    issues: list[str],
    pct_change: float,
) -> str:
    """生成一句话综合评价。"""
    if opinion == "排除":
        major = [i for i in issues if i.startswith("[矛盾]")]
        if major:
            return f"{name}：存在{len(major)}个矛盾信号，建议排除"
        return f"{name}：风险等级{risk_level}，不建议参与"

    if opinion == "谨慎":
        return f"{name}：风险等级{risk_level}，{'R/R=' + str(risk_reward) + '，' if risk_reward > 0 else ''}需等回调或小仓位"

    # 建议关注
    parts = [f"{name}：风险等级{risk_level}"]
    if risk_reward > 0:
        parts.append(f"R/R={risk_reward}")
    if highlights:
        parts.append(highlights[0])
    if pct_change > 5:
        parts.append(f"涨幅{pct_change:.1f}%偏高")
    return "，".join(parts)


# ---------------------------------------------------------------------------
# opinion 判定（增强版）
# ---------------------------------------------------------------------------


def _determine_opinion(
    all_issues: list[str],
    highlights: list[str],
    risk_reward: float,
    pct_change: float,
) -> tuple[str, int]:
    """根据审核结果判定最终意见和风险等级。

    返回 (opinion, risk_level)。
    risk_level: 0=无风险, 1=轻微, 2=注意, 3=有风险, 4=高风险, 5=极高风险

    亮点可以抵消部分轻微/可疑问题（每个亮点抵消1个轻微问题），但不影响矛盾问题。
    """
    # 统计问题严重程度
    major_issues = [i for i in all_issues if i.startswith("[矛盾]")]
    minor_issues = [i for i in all_issues if i.startswith("[可疑]") or i.startswith("[轻微]")]
    normal_issues = [i for i in all_issues if not i.startswith(("[矛盾]", "[可疑]", "[轻微]"))]

    total_major = len(major_issues)
    total_minor = len(minor_issues)
    total_normal = len(normal_issues)

    # 亮点抵消轻微问题
    highlight_count = len(highlights)
    offset_minor = min(total_minor, highlight_count)
    effective_minor = total_minor - offset_minor

    # 计算风险等级
    risk_level = 0
    if total_major >= 2:
        risk_level = 5
    elif total_major >= 1 and total_normal >= 2:
        risk_level = 4
    elif total_major >= 1:
        risk_level = 3
    elif total_normal >= 3:
        risk_level = 3
    elif total_normal >= 2 or (effective_minor >= 2 and total_normal >= 1):
        risk_level = 2
    elif total_normal >= 1 or effective_minor >= 1:
        risk_level = 1

    # 涨幅过高直接提升风险
    if pct_change > 9:
        risk_level = max(risk_level, 4)
    elif pct_change > 7:
        risk_level = max(risk_level, 2)

    # 风险收益比过低提升风险
    if 0 < risk_reward < 0.5:
        risk_level = max(risk_level, 2)

    # 有3+亮点时，风险等级可以下调1级（最低到1）
    if highlight_count >= 3 and risk_level >= 2 and total_major == 0:
        risk_level = max(1, risk_level - 1)

    # 判定 opinion
    if risk_level >= 4:
        return "排除", risk_level
    elif risk_level >= 2:
        return "谨慎", risk_level
    else:
        return "建议关注", risk_level


# ---------------------------------------------------------------------------
# 主审核函数
# ---------------------------------------------------------------------------


def review_candidates(candidates: list[dict]) -> BatchReview:
    """对候选股列表执行全面审核。"""
    if not candidates:
        return BatchReview(
            reviews=[],
            summary="无候选股数据，跳过审核。",
            warnings=[],
            recommendations=[],
            stats={"total": 0},
        )

    rank_map = _compute_ranks(candidates)

    reviews: list[CandidateReview] = []
    all_pct: list[float] = []
    all_scores: list[float] = []
    prefix_counter: Counter = Counter()
    sector_counter: Counter = Counter()
    high_pct_count = 0
    negative_pct_count = 0
    conflict_candidate_count = 0

    for idx, row in enumerate(candidates):
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("name", "")).strip()
        pct_change = _to_float(row.get("pct_change"))
        volume_ratio = _to_float(row.get("volume_ratio"))
        score = _to_float(row.get("candidate_score"))
        risk = str(row.get("risk", "")).strip()
        tags = _parse_tags(row.get("tags"))
        rank = rank_map.get(idx, idx + 1)

        all_issues: list[str] = []

        # 维度1: 标签矛盾
        tag_issues = _detect_tag_conflicts(tags, pct_change)
        all_issues.extend(tag_issues)
        if tag_issues:
            conflict_candidate_count += 1

        # 维度2: 评分合理性
        all_issues.extend(_check_score_rationality(row, tags, rank))

        # 维度5: 量价异常
        all_issues.extend(_check_volume_price(volume_ratio, pct_change))

        # 维度6: 技术指标一致性
        all_issues.extend(_check_technical_consistency(row, tags))

        # 维度7: 图形质量对齐
        all_issues.extend(_check_chart_quality_alignment(row))

        # 维度8: 板块弱势高分
        all_issues.extend(_check_sector_weakness(row))

        # 维度9: 支撑压力位 + 风险收益比
        sr_issues, risk_reward = _check_support_resistance(row)
        all_issues.extend(sr_issues)

        # 维度10: 亮点识别
        highlights = _identify_highlights(row, tags, risk_reward)

        # 维度11: 高位风险
        all_issues.extend(_check_position_risk(row, pct_change))

        # 维度12: 趋势完整性
        all_issues.extend(_check_trend_integrity(row, tags))

        # 维度13: 流动性风险
        all_issues.extend(_check_liquidity_risk(row, volume_ratio))

        # opinion 判定
        opinion, risk_level = _determine_opinion(all_issues, highlights, risk_reward, pct_change)

        # 仓位建议
        plan_position_pct = _to_float(row.get("suggested_position_pct"), 10)
        position_suggestion = _generate_position_suggestion(
            opinion, risk_level, risk_reward, pct_change, plan_position_pct
        )

        # 一句话简评
        brief = _generate_brief(
            name, opinion, risk_level, risk_reward, highlights, all_issues, pct_change
        )

        reviews.append(
            CandidateReview(
                symbol=symbol,
                name=name,
                opinion=opinion,
                issues=all_issues,
                highlights=highlights,
                score=score,
                risk_reward=risk_reward,
                risk_level=risk_level,
                position_suggestion=position_suggestion,
                brief=brief,
            )
        )

        # 全局统计
        all_pct.append(pct_change)
        all_scores.append(score)
        prefix = symbol[:3] if len(symbol) >= 3 else symbol
        prefix_counter[prefix] += 1
        sector_name = str(row.get("sector", "")).strip()
        if sector_name and sector_name != "未知":
            sector_counter[sector_name] += 1
        if pct_change > 7:
            high_pct_count += 1
        if pct_change < 0:
            negative_pct_count += 1

    # ------------------------------------------------------------------
    # 全局维度 → warnings
    # ------------------------------------------------------------------
    warnings: list[str] = []
    total = len(candidates)

    # 板块集中度
    concentration_counter = sector_counter if sector_counter else prefix_counter
    concentration_label = "行业" if sector_counter else "前缀"
    if concentration_counter:
        top_item, top_count = concentration_counter.most_common(1)[0]
        top_ratio = top_count / total
        if top_ratio > 0.3:
            warnings.append(
                f"板块集中度风险：{concentration_label}「{top_item}」占比{top_ratio*100:.1f}%（{top_count}/{total}）> 30%"
            )
        top3 = concentration_counter.most_common(3)
        top3_count = sum(c for _, c in top3)
        top3_ratio = top3_count / total
        top3_desc = "、".join(f"{p}({c})" for p, c in top3)
        if top3_ratio > 0.6:
            warnings.append(
                f"高度集中风险：前3大{concentration_label} {top3_desc} 合计{top3_ratio*100:.1f}%（{top3_count}/{total}）> 60%"
            )

    # 涨幅分布
    if all_pct:
        avg_pct = sum(all_pct) / len(all_pct)
        if avg_pct > 5:
            warnings.append(f"整体涨幅偏高：平均{avg_pct:.2f}% > 5%，追高风险")

        high_ratio = high_pct_count / total
        if high_ratio > 0.3:
            warnings.append(f"高位股比例过高：涨幅>7%占比{high_ratio*100:.1f}%（{high_pct_count}/{total}）> 30%")

        if negative_pct_count == 0:
            warnings.append("缺少低位布局标的：无涨幅为负的候选")

        # 评分集中度
        avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
        if avg_score > 130:
            warnings.append(f"整体评分偏高：平均{avg_score:.0f}分，可能存在系统性偏差")

    # ------------------------------------------------------------------
    # 操作建议 recommendations
    # ------------------------------------------------------------------
    recommendations: list[str] = []

    # 推荐关注：建议关注 + 有亮点
    recommended = [r for r in reviews if r.opinion == "建议关注" and r.highlights]
    if recommended:
        top_recs = sorted(recommended, key=lambda r: r.score, reverse=True)[:3]
        rec_names = "、".join(f"{r.symbol}({r.name})" for r in top_recs)
        recommendations.append(f"优先关注：{rec_names}（多信号共振+基本面良好）")

    # 推荐关注但无亮点
    recommended_plain = [r for r in reviews if r.opinion == "建议关注" and not r.highlights]
    if recommended_plain:
        plain_names = "、".join(f"{r.symbol}" for r in recommended_plain[:3])
        recommendations.append(f"可关注：{plain_names}（指标正常但无明显共振信号）")

    # 谨慎标的
    cautious = [r for r in reviews if r.opinion == "谨慎"]
    if cautious:
        caut_names = "、".join(f"{r.symbol}" for r in cautious[:5])
        recommendations.append(f"谨慎参与：{caut_names}（存在风险因素，建议小仓位或等回调）")

    # 排除标的
    excluded = [r for r in reviews if r.opinion == "排除"]
    if excluded:
        excl_names = "、".join(f"{r.symbol}" for r in excluded)
        recommendations.append(f"建议排除：{excl_names}（存在重大风险信号）")

    # 高风险收益比标的
    good_rr = [r for r in reviews if r.risk_reward >= 2.0 and r.opinion != "排除"]
    if good_rr:
        rr_names = "、".join(f"{r.symbol}(R/R={r.risk_reward})" for r in good_rr[:3])
        recommendations.append(f"风险收益比优秀：{rr_names}")

    # 仓位管理建议
    high_risk = [r for r in reviews if r.risk_level >= 3]
    if len(high_risk) > total * 0.3:
        recommendations.append(f"仓位管理：{len(high_risk)}/{total}只标的为高风险，建议整体仓位控制在50%以下")

    # ------------------------------------------------------------------
    # 摘要与统计
    # ------------------------------------------------------------------
    opinion_counter = Counter(r.opinion for r in reviews)
    summary_parts = [f"共审核{total}只候选股"]
    for op in ("建议关注", "谨慎", "排除"):
        count = opinion_counter.get(op, 0)
        if count > 0:
            summary_parts.append(f"{op}{count}只")
    if warnings:
        summary_parts.append(f"全局风险{len(warnings)}条")
    if recommendations:
        summary_parts.append(f"操作建议{len(recommendations)}条")
    summary = "，".join(summary_parts) + "。"

    stats = {
        "total": total,
        "opinion_counts": dict(opinion_counter),
        "risk_level_distribution": dict(Counter(r.risk_level for r in reviews)),
        "avg_pct_change": round(sum(all_pct) / len(all_pct), 2) if all_pct else 0.0,
        "avg_score": round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0,
        "sector_distribution": dict(sector_counter.most_common(5)) if sector_counter else {},
        "high_pct_count": high_pct_count,
        "negative_pct_count": negative_pct_count,
        "conflict_candidate_count": conflict_candidate_count,
        "avg_risk_reward": round(sum(r.risk_reward for r in reviews if r.risk_reward > 0) / max(1, sum(1 for r in reviews if r.risk_reward > 0)), 2),
        "highlight_count": sum(len(r.highlights) for r in reviews),
        "high_risk_count": sum(1 for r in reviews if r.risk_level >= 3),
    }

    return BatchReview(
        reviews=reviews,
        summary=summary,
        warnings=warnings,
        recommendations=recommendations,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# CSV 加载辅助
# ---------------------------------------------------------------------------


def load_candidates_from_csv(csv_path) -> list[dict]:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [row for row in csv.DictReader(f) if row]


# ---------------------------------------------------------------------------
# 直接运行
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys

    project_root = Path(__file__).resolve().parents[2]
    default_csv = project_root / "outputs" / "daily_candidates.csv"
    csv_path = sys.argv[1] if len(sys.argv) > 1 else str(default_csv)

    if not Path(csv_path).exists():
        print(f"[错误] 候选股 CSV 不存在：{csv_path}")
        sys.exit(1)

    candidates = load_candidates_from_csv(csv_path)
    print(f"[加载] 共读取 {len(candidates)} 只候选股：{csv_path}")

    batch = review_candidates(candidates)

    print("\n" + "=" * 70)
    print("审核摘要")
    print("=" * 70)
    print(batch.summary)

    print("\n" + "-" * 70)
    print("全局风险提示")
    print("-" * 70)
    if batch.warnings:
        for i, w in enumerate(batch.warnings, 1):
            print(f"  {i}. {w}")
    else:
        print("  （无）")

    print("\n" + "-" * 70)
    print("操作建议")
    print("-" * 70)
    if batch.recommendations:
        for i, r in enumerate(batch.recommendations, 1):
            print(f"  {i}. {r}")
    else:
        print("  （无）")

    print("\n" + "-" * 70)
    print("逐只审核结果")
    print("-" * 70)
    for r in batch.reviews:
        print(f"\n  {r.symbol} {r.name}  |  评分{r.score}  |  R/R={r.risk_reward}  |  风险等级{r.risk_level}  |  {r.opinion}")
        print(f"    简评：{r.brief}")
        print(f"    仓位：{r.position_suggestion}")
        if r.highlights:
            for h in r.highlights:
                print(f"    ★ {h}")
        if r.issues:
            for issue in r.issues:
                print(f"    - {issue}")
        if not r.issues and not r.highlights:
            print("    - （无异常，无亮点）")
