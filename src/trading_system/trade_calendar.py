"""A股交易日历模块。

基于 AkShare 的 tool_trade_date_hist_sina 接口，数据来自新浪财经。
支持本地缓存，避免重复请求。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd

_CACHE: dict[str, object] = {
    "dates": None,         # set[date]
    "sorted_dates": None,  # list[date] 升序
    "fetched_at": 0.0,
    "ttl": 86400 * 7,      # 缓存7天，交易日历不会变
}

_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "trade_calendar.csv"


def _load_from_cache_file() -> Optional[set[date]]:
    """从本地 CSV 缓存加载交易日历。"""
    if not _CACHE_FILE.exists():
        return None
    try:
        df = pd.read_csv(_CACHE_FILE)
        if "trade_date" not in df.columns:
            return None
        dates = {datetime.strptime(str(d), "%Y-%m-%d").date() for d in df["trade_date"].dropna()}
        return dates
    except Exception:
        return None


def _save_to_cache_file(dates: set[date]) -> None:
    """保存交易日历到本地 CSV 缓存。"""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        sorted_dates = sorted(dates)
        pd.DataFrame({"trade_date": [d.isoformat() for d in sorted_dates]}).to_csv(_CACHE_FILE, index=False)
    except Exception:
        pass


def _fetch_from_akshare() -> Optional[set[date]]:
    """从 AkShare 拉取交易日历。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty or "trade_date" not in df.columns:
            return None
        dates = {pd.Timestamp(d).date() for d in df["trade_date"].dropna()}
        return dates
    except Exception:
        return None


def _ensure_loaded() -> set[date]:
    """确保交易日历已加载，返回交易日集合。"""
    now = time.time()
    if _CACHE["dates"] is not None and (now - _CACHE["fetched_at"]) < _CACHE["ttl"]:
        return _CACHE["dates"]  # type: ignore[return-value]

    # 先试本地缓存
    dates = _load_from_cache_file()
    if dates:
        _CACHE["dates"] = dates
        _CACHE["sorted_dates"] = sorted(dates)
        _CACHE["fetched_at"] = now
        return dates

    # 本地缓存没有，从 AkShare 拉
    dates = _fetch_from_akshare()
    if dates:
        _CACHE["dates"] = dates
        _CACHE["sorted_dates"] = sorted(dates)
        _CACHE["fetched_at"] = now
        _save_to_cache_file(dates)
        return dates

    # 都失败了，回退到简单的周末判断
    return set()


def _fallback_is_trading_day(d: date) -> bool:
    """回退方案：仅按周一至周五判断。"""
    return d.weekday() < 5


def is_trading_day(d: date | None = None) -> bool:
    """判断指定日期是否为交易日。

    Args:
        d: 日期，默认为今天。

    Returns:
        True 表示是交易日。
    """
    d = d or date.today()
    dates = _ensure_loaded()
    if dates:
        return d in dates
    return _fallback_is_trading_day(d)


def last_trading_day(d: date | None = None) -> date:
    """返回最近一个交易日（含当天如果当天是交易日）。

    Args:
        d: 基准日期，默认为今天。

    Returns:
        最近的交易日 date。
    """
    d = d or date.today()
    dates = _ensure_loaded()
    if not dates:
        # 回退到周末判断
        result = d
        while result.weekday() >= 5:
            result -= timedelta(days=1)
        return result

    sorted_dates = _CACHE["sorted_dates"]  # type: ignore[assignment]
    if d in dates:
        return d
    # 往前找最近的交易日
    for td in reversed(sorted_dates):
        if td < d:
            return td
    # 理论上不会走到这里
    return d


def next_trading_day(d: date | None = None) -> date:
    """返回下一个交易日（不含当天）。

    Args:
        d: 基准日期，默认为今天。

    Returns:
        下一个交易日 date。
    """
    d = d or date.today()
    dates = _ensure_loaded()
    if not dates:
        result = d + timedelta(days=1)
        while result.weekday() >= 5:
            result += timedelta(days=1)
        return result

    sorted_dates = _CACHE["sorted_dates"]  # type: ignore[assignment]
    for td in sorted_dates:
        if td > d:
            return td
    return d


def shift_trading_days(d: date, n: int) -> date:
    """向前/向后推移 n 个交易日。

    Args:
        d: 基准日期。
        n: 推移天数，正数往后推，负数往前推。

    Returns:
        推移后的交易日 date。
    """
    dates = _ensure_loaded()
    if not dates:
        # 回退：按工作日推移
        result = d
        step = 1 if n > 0 else -1
        remaining = abs(n)
        while remaining > 0:
            result += timedelta(days=step)
            if result.weekday() < 5:
                remaining -= 1
        return result

    sorted_dates = _CACHE["sorted_dates"]  # type: ignore[assignment]
    # 找到 d 在列表中的位置
    try:
        idx = sorted_dates.index(d)
    except ValueError:
        # d 不在列表中，找第一个大于 d 的位置
        idx = 0
        for i, td in enumerate(sorted_dates):
            if td > d:
                idx = i - 1 if n < 0 else i
                break
        else:
            idx = len(sorted_dates) - 1 if n < 0 else len(sorted_dates)

    target_idx = idx + n
    target_idx = max(0, min(target_idx, len(sorted_dates) - 1))
    return sorted_dates[target_idx]


def trading_days_between(start: date, end: date) -> List[date]:
    """返回指定日期范围内的所有交易日（含两端）。"""
    dates = _ensure_loaded()
    if not dates:
        result = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                result.append(current)
            current += timedelta(days=1)
        return result

    sorted_dates = _CACHE["sorted_dates"]  # type: ignore[assignment]
    return [d for d in sorted_dates if start <= d <= end]


def reload() -> None:
    """强制重新从 AkShare 拉取交易日历。"""
    _CACHE["dates"] = None
    _CACHE["sorted_dates"] = None
    _CACHE["fetched_at"] = 0.0
    dates = _fetch_from_akshare()
    if dates:
        _CACHE["dates"] = dates
        _CACHE["sorted_dates"] = sorted(dates)
        _CACHE["fetched_at"] = time.time()
        _save_to_cache_file(dates)
