from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


@dataclass
class Rulebook:
    raw: Dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "Rulebook":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @property
    def market_timing(self) -> Dict[str, Any]:
        return self.raw["market_timing"]

    @property
    def position_policy(self) -> Dict[str, Any]:
        return self.raw["position_policy"]

    @property
    def risk_priority(self) -> List[str]:
        return self.raw["risk_priority"]

    @property
    def tag_signal_map(self) -> Dict[str, Dict[str, Any]]:
        return self.raw["tag_signal_map"]

    def tag_score(self, tag: str) -> int:
        return int(self.tag_signal_map.get(tag, {}).get("base_score", 0))

    def tag_direction(self, tag: str) -> str:
        return str(self.tag_signal_map.get(tag, {}).get("direction", "context"))

    @property
    def signal_scoring(self) -> Dict[str, Any]:
        return self.raw.get("signal_scoring", {})

    @property
    def signal_bonus(self) -> Dict[str, int]:
        return self.signal_scoring.get("signal_bonus", {})

    @property
    def combo_bonus(self) -> Dict[str, int]:
        return self.signal_scoring.get("combo_bonus", {})

    @property
    def regime_adjustment(self) -> Dict[str, Dict[str, Any]]:
        return self.signal_scoring.get("regime_adjustment", {})

    @property
    def regime_signal_filter(self) -> Dict[str, List[str]]:
        return self.signal_scoring.get("regime_signal_filter", {})

    def regime_penalty(self, regime_name: str) -> int:
        entry = self.regime_adjustment.get(regime_name, {})
        return int(entry.get("penalty", 0))

    def regime_description(self, regime_name: str) -> str:
        entry = self.regime_adjustment.get(regime_name, {})
        return str(entry.get("description", ""))

    def allowed_signals(self, regime_name: str) -> List[str]:
        return self.regime_signal_filter.get(regime_name, [])

    @property
    def pass_score(self) -> int:
        return int(self.signal_scoring.get("pass_score", 30))

    def pass_score_for_regime(self, regime_name: str) -> int:
        by_regime = self.signal_scoring.get("pass_score_by_regime", {})
        return int(by_regime.get(regime_name, self.pass_score))

    @property
    def backtest_config(self) -> Dict[str, Any]:
        return self.raw.get("backtest", {})

    @property
    def chart_quality_bonus(self) -> Dict[str, Any]:
        return self.signal_scoring.get("chart_quality_bonus", {})

    def chart_quality_adjustment(self, chart_score: int) -> Tuple[int, str]:
        cfg = self.chart_quality_bonus
        exc_thresh = int(cfg.get("excellent_threshold", 80))
        exc_bonus = int(cfg.get("excellent_bonus", 10))
        good_thresh = int(cfg.get("good_threshold", 60))
        good_bonus = int(cfg.get("good_bonus", 6))
        poor_thresh = int(cfg.get("poor_threshold", 40))
        poor_penalty = int(cfg.get("poor_penalty", -5))
        if chart_score >= exc_thresh:
            return exc_bonus, f"图形质量优秀({chart_score}分)"
        elif chart_score >= good_thresh:
            return good_bonus, f"图形质量良好({chart_score}分)"
        elif chart_score < poor_thresh:
            return poor_penalty, f"图形质量较差({chart_score}分)"
        return 0, ""

    def combo_bonus_for(self, combo_name: str) -> int:
        return int(self.combo_bonus.get(combo_name, 0))
