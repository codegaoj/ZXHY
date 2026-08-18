from __future__ import annotations

from typing import List, Optional

from . import indicators
from .models import (
    MarketRegime,
    MarketState,
    RiskAction,
    SecuritySnapshot,
    Signal,
    SignalDirection,
    TradePlan,
)
from .rulebook import Rulebook


class MarketTimingEngine:
    def __init__(self, rulebook: Rulebook):
        self.rulebook = rulebook

    def evaluate(
        self,
        active_market_value_pct: float,
        macd_dif: float = 0.0,
        recent_values: Optional[List[float]] = None,
        manual_override: Optional[str] = None,
    ) -> MarketState:
        cfg = self.rulebook.market_timing
        bull = float(cfg["bull_threshold"])
        bear = float(cfg["bear_threshold"])
        macd_above_zero = macd_dif > 0

        if manual_override:
            return self._apply_override(manual_override, active_market_value_pct, macd_above_zero, recent_values, cfg)

        # 单日多头判定
        single_day_bull = active_market_value_pct >= bull

        # 3日累计多头判定
        cumulative_bull = False
        cumulative_detail = ""
        cum_days = int(cfg.get("cumulative_days", 3))
        cum_threshold = float(cfg.get("cumulative_threshold", 4.0))
        if recent_values and len(recent_values) >= 1:
            recent_slice = recent_values[-cum_days:]
            cum_sum = round(sum(recent_slice), 4)
            if cum_sum >= cum_threshold:
                cumulative_bull = True
                cumulative_detail = f"近{len(recent_slice)}日活跃市值累计 {cum_sum:.2f}% >= {cum_threshold:.1f}%"

        is_bull = (single_day_bull or cumulative_bull) and macd_above_zero

        if is_bull:
            regime = MarketRegime.BULL
            if single_day_bull:
                reason = f"活跃市值单日 {active_market_value_pct:.2f}% >= {bull:.1f}%，且 MACD 在零轴上方，允许进攻。"
            else:
                reason = f"{cumulative_detail}，且 MACD 在零轴上方，允许进攻。"
        elif active_market_value_pct <= bear:
            regime = MarketRegime.BEAR
            reason = f"活跃市值单日 {active_market_value_pct:.2f}% <= {bear:.1f}%，进入空头阈值，原则上只卖不买。"
        else:
            regime = self._classify_neutral(active_market_value_pct, cfg)
            reason = self._neutral_reason(regime, active_market_value_pct, recent_values, cfg)

        policy = self.rulebook.position_policy[regime.policy_key]
        return MarketState(
            regime=regime,
            active_market_value_pct=active_market_value_pct,
            macd_above_zero=macd_above_zero,
            reason=reason,
            max_position_pct=int(policy["max_position_pct"]),
            single_symbol_pct=int(policy["single_symbol_pct"]),
            manual_override=False,
        )

    def _apply_override(self, override: str, amv: float, macd_above: bool, recent_values, cfg) -> MarketState:
        override_map = {
            "bull": MarketRegime.BULL,
            "neutral_strong": MarketRegime.NEUTRAL_STRONG,
            "neutral_balanced": MarketRegime.NEUTRAL_BALANCED,
            "neutral_weak": MarketRegime.NEUTRAL_WEAK,
            "bear": MarketRegime.BEAR,
        }
        regime = override_map.get(override, MarketRegime.NEUTRAL_BALANCED)
        policy = self.rulebook.position_policy[regime.policy_key]
        reason = f"[手动覆盖] 当前市场状态设为：{regime.value}（活跃市值 {amv:.2f}%）。"
        return MarketState(
            regime=regime,
            active_market_value_pct=amv,
            macd_above_zero=macd_above,
            reason=reason,
            max_position_pct=int(policy["max_position_pct"]),
            single_symbol_pct=int(policy["single_symbol_pct"]),
            manual_override=True,
        )

    @staticmethod
    def _classify_neutral(amv: float, cfg: dict) -> MarketRegime:
        strong_threshold = float(cfg.get("neutral_strong_threshold", 1.0))
        weak_threshold = float(cfg.get("neutral_weak_threshold", -0.5))
        if amv >= strong_threshold:
            return MarketRegime.NEUTRAL_STRONG
        if amv <= weak_threshold:
            return MarketRegime.NEUTRAL_WEAK
        return MarketRegime.NEUTRAL_BALANCED

    @staticmethod
    def _neutral_reason(regime: MarketRegime, amv: float, recent_values, cfg: dict) -> str:
        parts = [f"活跃市值单日 {amv:.2f}%"]
        cum_days = int(cfg.get("cumulative_days", 3))
        if recent_values and len(recent_values) >= 1:
            recent_slice = recent_values[-cum_days:]
            cum_sum = round(sum(recent_slice), 4)
            parts.append(f"近{len(recent_slice)}日累计 {cum_sum:.2f}%")
        if regime == MarketRegime.NEUTRAL_STRONG:
            parts.append("震荡偏强，可半仓操作B1，B2谨慎参与。")
        elif regime == MarketRegime.NEUTRAL_WEAK:
            parts.append("震荡偏弱，仓位压至20%以下，仅做B1，蹬起来就卖。")
        else:
            parts.append("震荡中性，仓位30%-35%，仅做完美B1，少动=赢。")
        return "，".join(parts)


class EntrySignalEngine:
    def __init__(self, rulebook: Rulebook):
        self.rulebook = rulebook

    def evaluate(self, snapshot: SecuritySnapshot, market: MarketState) -> List[Signal]:
        signals: List[Signal] = []
        score = sum(self.rulebook.tag_score(tag) for tag in snapshot.tags)

        if "B1/B2买点" in snapshot.tags and indicators.near_support(snapshot):
            signals.append(Signal(
                symbol=snapshot.symbol,
                name=snapshot.name,
                direction=SignalDirection.ENTRY,
                signal_type="B1/B2候选",
                score=score + 10,
                reason="样本标签命中 B1/B2，且价格接近支撑位。",
                evidence={"support_price": snapshot.support_price, "close": snapshot.close},
            ))

        if "关键K" in snapshot.tags and indicators.is_volume_breakout(snapshot):
            signals.append(Signal(
                symbol=snapshot.symbol,
                name=snapshot.name,
                direction=SignalDirection.ENTRY,
                signal_type="关键K放量",
                score=score + 8,
                reason="关键K标签命中，且量比放大、价格上涨。",
                evidence={"volume_ratio": round(snapshot.volume_ratio, 2), "pct_change": round(snapshot.pct_change, 2)},
            ))

        if "双线/白黄线" in snapshot.tags and indicators.is_above_white_line(snapshot) and indicators.is_above_yellow_line(snapshot):
            signals.append(Signal(
                symbol=snapshot.symbol,
                name=snapshot.name,
                direction=SignalDirection.HOLD,
                signal_type="双线支撑",
                score=score,
                reason="价格位于白线和黄线之上，趋势结构未破坏。",
                evidence={"white_line": snapshot.white_line, "yellow_line": snapshot.yellow_line},
            ))

        if market.regime == MarketRegime.BEAR:
            for signal in signals:
                signal.score = max(0, signal.score - 25)
                signal.reason += " 但市场处于空头区间，进攻分数下调。"
        elif market.regime == MarketRegime.NEUTRAL_WEAK:
            for signal in signals:
                signal.score = max(0, signal.score - 12)
                signal.reason += " 震荡偏弱区间，进攻分数适度下调。"

        return signals


class RiskEngine:
    def __init__(self, rulebook: Rulebook):
        self.rulebook = rulebook

    def _priority(self, rule_name: str) -> int:
        try:
            return self.rulebook.risk_priority.index(rule_name) + 1
        except ValueError:
            return 99

    def evaluate(self, snapshot: SecuritySnapshot) -> List[RiskAction]:
        actions: List[RiskAction] = []

        if snapshot.close < snapshot.support_price:
            actions.append(RiskAction(
                symbol=snapshot.symbol,
                action="止损/退出",
                priority=self._priority("初始止损"),
                reason="收盘价跌破支撑价，触发初始止损。",
                triggered_rules=["初始止损"],
            ))

        if snapshot.close < snapshot.white_line:
            actions.append(RiskAction(
                symbol=snapshot.symbol,
                action="减仓",
                priority=self._priority("破白线"),
                reason="收盘价跌破白线，短期趋势破坏。",
                triggered_rules=["破白线"],
            ))

        if snapshot.close < snapshot.yellow_line:
            actions.append(RiskAction(
                symbol=snapshot.symbol,
                action="清仓/回避",
                priority=self._priority("破黄线"),
                reason="收盘价跌破黄线，主力成本线失守。",
                triggered_rules=["破黄线"],
            ))

        if "S1/卖点" in snapshot.tags and snapshot.pct_change < -2:
            actions.append(RiskAction(
                symbol=snapshot.symbol,
                action="防守卖出",
                priority=self._priority("S1/卖点"),
                reason="标签命中 S1/卖点，且当日跌幅较大。",
                triggered_rules=["S1/卖点"],
            ))

        if indicators.is_macd_veto(snapshot):
            actions.append(RiskAction(
                symbol=snapshot.symbol,
                action="否决开仓",
                priority=self._priority("MACD否决"),
                reason="MACD 位于弱势且 DIF 低于 DEA，否决新增开仓。",
                triggered_rules=["MACD否决"],
            ))

        actions.sort(key=lambda item: item.priority)
        return actions


class TradePlanEngine:
    def __init__(self, rulebook: Rulebook):
        self.market_engine = MarketTimingEngine(rulebook)
        self.entry_engine = EntrySignalEngine(rulebook)
        self.risk_engine = RiskEngine(rulebook)

    def build_plan(self, snapshot: SecuritySnapshot, market_override: Optional[str] = None) -> TradePlan:
        market = self.market_engine.evaluate(
            snapshot.active_market_value_pct,
            snapshot.macd_dif,
            snapshot.recent_active_market_values,
            manual_override=market_override,
        )
        entry_signals = self.entry_engine.evaluate(snapshot, market)
        risk_actions = self.risk_engine.evaluate(snapshot)

        score = max([signal.score for signal in entry_signals], default=0)
        notes = [market.reason]

        if risk_actions:
            top_risk = risk_actions[0]
            action = top_risk.action
            suggested_position_pct = 0 if "清仓" in action or "退出" in action or "否决" in action else max(0, market.single_symbol_pct // 2)
            notes.append(f"风控优先：{top_risk.reason}")
        elif market.regime == MarketRegime.BEAR:
            action = "观察/不开新仓"
            suggested_position_pct = 0
            notes.append("空头区间不因买点信号主动进攻。")
        elif market.regime == MarketRegime.NEUTRAL_WEAK:
            if score >= 55:
                action = "观察/仅B1轻仓"
                suggested_position_pct = market.single_symbol_pct
                notes.append("震荡偏弱，仅B1信号可轻仓试探，快进快出。")
            else:
                action = "观察/不开新仓"
                suggested_position_pct = 0
                notes.append("震荡偏弱，信号不足，保守观望。")
        elif market.regime == MarketRegime.NEUTRAL_BALANCED:
            if score >= 55:
                action = "可纳入买入候选"
                suggested_position_pct = market.single_symbol_pct
                notes.append("震荡中性，B1信号可参与，注意仓位控制。")
            elif entry_signals:
                action = "观察等待确认"
                suggested_position_pct = max(0, market.single_symbol_pct // 2)
                notes.append("震荡中性，有信号但强度不足，等待确认。")
            else:
                action = "无操作"
                suggested_position_pct = 0
                notes.append("震荡中性，未出现足够清晰的进攻信号。")
        elif score >= 55:
            action = "可纳入买入候选"
            suggested_position_pct = market.single_symbol_pct
            notes.append("进攻信号较强，但仍需人工复核图形。")
        elif entry_signals:
            action = "观察等待确认"
            suggested_position_pct = max(0, market.single_symbol_pct // 2)
            notes.append("有信号但强度不足，等待回踩或放量确认。")
        else:
            action = "无操作"
            suggested_position_pct = 0
            notes.append("未出现足够清晰的进攻信号。")

        return TradePlan(
            symbol=snapshot.symbol,
            name=snapshot.name,
            market_regime=market.regime,
            action=action,
            suggested_position_pct=suggested_position_pct,
            score=score,
            entry_signals=entry_signals,
            risk_actions=risk_actions,
            notes=notes,
        )
