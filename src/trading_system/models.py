from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class MarketRegime(str, Enum):
    BULL = "多头"
    NEUTRAL = "震荡"
    BEAR = "空头"


class SignalDirection(str, Enum):
    ENTRY = "买入候选"
    EXIT = "卖出/防守"
    HOLD = "持有/观察"
    VETO = "否决"


@dataclass
class GraphSample:
    id: str
    title: str
    sample_type: str
    tags: List[str]
    source_file: str = ""
    related_doc: str = ""
    manual_note: str = ""
    image: str = ""
    thumbnail: str = ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "GraphSample":
        return cls(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            sample_type=str(raw.get("sample_type", "")),
            tags=list(raw.get("tags", [])),
            source_file=str(raw.get("source_file", "")),
            related_doc=str(raw.get("related_doc", "")),
            manual_note=str(raw.get("manual_note", "")),
            image=str(raw.get("image", "")),
            thumbnail=str(raw.get("thumbnail", "")),
        )


@dataclass
class SecuritySnapshot:
    symbol: str
    name: str
    close: float
    prev_close: float
    volume: float
    volume_ma5: float
    active_market_value_pct: float
    macd_dif: float
    macd_dea: float
    white_line: float
    yellow_line: float
    support_price: float
    resistance_price: float
    tags: List[str] = field(default_factory=list)
    recent_active_market_values: List[float] = field(default_factory=list)

    @property
    def pct_change(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.close / self.prev_close - 1) * 100

    @property
    def volume_ratio(self) -> float:
        if self.volume_ma5 == 0:
            return 0.0
        return self.volume / self.volume_ma5


@dataclass
class MarketState:
    regime: MarketRegime
    active_market_value_pct: float
    macd_above_zero: bool
    reason: str
    max_position_pct: int
    single_symbol_pct: int


@dataclass
class Signal:
    symbol: str
    name: str
    direction: SignalDirection
    signal_type: str
    score: int
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskAction:
    symbol: str
    action: str
    priority: int
    reason: str
    triggered_rules: List[str] = field(default_factory=list)


@dataclass
class TradePlan:
    symbol: str
    name: str
    market_regime: MarketRegime
    action: str
    suggested_position_pct: int
    score: int
    entry_signals: List[Signal] = field(default_factory=list)
    risk_actions: List[RiskAction] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["market_regime"] = self.market_regime.value
        for sig in data["entry_signals"]:
            sig["direction"] = sig["direction"].value
        return data
