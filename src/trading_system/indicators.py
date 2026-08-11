from __future__ import annotations

from .models import SecuritySnapshot


def is_volume_breakout(snapshot: SecuritySnapshot, threshold: float = 1.8) -> bool:
    return snapshot.volume_ratio >= threshold and snapshot.close > snapshot.prev_close


def is_above_white_line(snapshot: SecuritySnapshot) -> bool:
    return snapshot.close >= snapshot.white_line


def is_above_yellow_line(snapshot: SecuritySnapshot) -> bool:
    return snapshot.close >= snapshot.yellow_line


def is_macd_bullish(snapshot: SecuritySnapshot) -> bool:
    return snapshot.macd_dif > 0 and snapshot.macd_dif >= snapshot.macd_dea


def is_macd_veto(snapshot: SecuritySnapshot) -> bool:
    return snapshot.macd_dif < 0 and snapshot.macd_dif < snapshot.macd_dea


def near_support(snapshot: SecuritySnapshot, tolerance_pct: float = 3.0) -> bool:
    if snapshot.support_price <= 0:
        return False
    distance = abs(snapshot.close / snapshot.support_price - 1) * 100
    return distance <= tolerance_pct
