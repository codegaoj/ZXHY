"""板块分析模块（多数据源）。

数据源：
1. 新浪行业（49板块）- 股票→行业映射基础
2. 巨潮资讯 stock_profile_cninfo - 补充未覆盖股票的行业信息
3. 同花顺行业汇总（90行业）- 行业实时表现（优先）
4. 新浪行业 - 行业实时表现（回退）

缓存策略：
    - 板块映射缓存7天（data/sector_mapping.csv）
    - CNINFO补充结果合并到同一缓存文件
    - 行业表现为实时数据不缓存
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SectorInfo:
    sector_name: str
    sector_change: float
    sector_rank: int
    sector_total_count: int
    strength: str  # "强势" / "中性" / "弱势"


def _try_import_akshare():
    try:
        import akshare as ak  # type: ignore
        return ak
    except Exception:
        return None


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() in {"", "-", "nan"}:
            return default
        return float(value)
    except Exception:
        return default


def _pure_code(raw: str) -> str:
    value = str(raw).strip().lower()
    if value.startswith(("sh", "sz", "bj")):
        value = value[2:]
    return value.zfill(6) if value.isdigit() and len(value) < 6 else value


# ---------------------------------------------------------------------------
# 行业名称标准化：将不同数据源的行业名映射到统一名称
# ---------------------------------------------------------------------------
_INDUSTRY_NORMALIZE = {
    # CNINFO(证监会) → 通用名
    "资本市场服务": "证券",
    "证券": "证券",
    "保险业": "保险",
    "保险": "保险",
    "房地产开发": "房地产",
    "房地产": "房地产",
    "房地产业": "房地产",
    "医药制造业": "化学制药",
    "化学药品原料药制造": "化学制药",
    "化学药品制剂制造": "化学制药",
    "中药饮片加工": "中药",
    "中成药生产": "中药",
    "生物药品制品制造": "生物制品",
    "计算机、通信和其他电子设备制造业": "消费电子",
    "软件和信息技术服务业": "软件开发",
    "互联网和相关服务": "互联网",
    "电信、广播电视和卫星传输服务": "通信设备",
    "电气机械和器材制造业": "电气设备",
    "汽车制造业": "汽车整车",
    "汽车零部件及配件制造": "汽车零部件",
    "专用设备制造业": "专用机械",
    "通用设备制造业": "通用机械",
    "非金属矿物制品业": "建材",
    "黑色金属冶炼和压延加工业": "钢铁",
    "有色金属冶炼和压延加工业": "有色金属",
    "金属制品业": "金属制品",
    "土木工程建筑业": "工程建筑",
    "房屋建筑业": "工程建筑",
    "批发业": "商业百货",
    "零售业": "商业百货",
    "农业": "种植业",
    "畜牧业": "养殖业",
    "农副食品加工业": "食品加工",
    "食品制造业": "食品饮料",
    "酒、饮料和精制茶制造业": "食品饮料",
    "纺织业": "纺织",
    "服装服饰业": "服装",
    "电力、热力生产和供应业": "电力",
    "燃气生产和供应业": "供气供热",
    "水的生产和供应业": "水务",
    "铁路运输业": "铁路公路",
    "道路运输业": "铁路公路",
    "航空运输业": "航空",
    "水上运输业": "港口水运",
    "装卸搬运和运输代理业": "物流",
    "邮政业": "物流",
    "商务服务业": "服务",
    "新闻和出版业": "传媒",
    "广播、电视、电影和录音制作业": "传媒",
    "文化艺术业": "传媒",
    "教育": "教育",
    "货币金融服务": "银行",
    "货币银行服务": "银行",
    "其他金融业": "多元金融",
    "租赁业": "租赁",
}


def _normalize_industry(name: str) -> str:
    """将不同数据源的行业名标准化为统一名称。"""
    name = str(name).strip()
    if name in _INDUSTRY_NORMALIZE:
        return _INDUSTRY_NORMALIZE[name]
    return name


# ---------------------------------------------------------------------------
# 缓存读写
# ---------------------------------------------------------------------------

def _read_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    mapping: dict[str, str] = {}
    try:
        with cache_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("code") or "").strip()
                sector = (row.get("sector") or "").strip()
                if code and sector:
                    mapping[code] = sector
    except Exception:
        return {}
    return mapping


def _write_cache(cache_path: Path, mapping: dict[str, str]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["code", "sector"])
            writer.writeheader()
            for code, sector in sorted(mapping.items()):
                writer.writerow({"code": code, "sector": sector})
    except Exception as exc:
        print(f"[板块映射] 写入缓存失败：{exc}")


# ---------------------------------------------------------------------------
# 新浪行业：构建基础映射
# ---------------------------------------------------------------------------

def _build_sina_mapping(ak) -> dict[str, str]:
    """通过新浪行业API构建股票→行业映射（49板块）。"""
    try:
        spot_df = ak.stock_sector_spot(indicator="新浪行业")
    except Exception as exc:
        print(f"[板块映射] 新浪行业板块列表失败：{exc}")
        return {}

    if spot_df is None or spot_df.empty:
        return {}

    label_col = "label" if "label" in spot_df.columns else spot_df.columns[0]
    name_col = "板块" if "板块" in spot_df.columns else spot_df.columns[1]

    mapping: dict[str, str] = {}
    total = len(spot_df)
    for idx, row in spot_df.iterrows():
        label = str(row[label_col]).strip()
        sector_name = str(row[name_col]).strip()  # 保留原始名称，不做标准化
        if not label or not sector_name:
            continue
        try:
            detail_df = ak.stock_sector_detail(sector=label)
        except Exception:
            continue
        if detail_df is None or detail_df.empty:
            continue
        code_col = "code" if "code" in detail_df.columns else None
        if code_col is None and "symbol" in detail_df.columns:
            for raw_code in detail_df["symbol"].astype(str):
                code = _pure_code(raw_code)
                if code.isdigit() and len(code) == 6:
                    mapping[code] = sector_name
        elif code_col:
            for raw_code in detail_df[code_col].astype(str):
                code = _pure_code(raw_code)
                if code.isdigit() and len(code) == 6:
                    mapping[code] = sector_name
        time.sleep(0.3)
        if (idx + 1) % 10 == 0:
            print(f"[板块映射] 新浪 {idx + 1}/{total}，累计 {len(mapping)} 只")
    return mapping


# ---------------------------------------------------------------------------
# 巨潮资讯：补充未覆盖股票的行业信息
# ---------------------------------------------------------------------------

def _supplement_with_cninfo(ak, mapping: dict[str, str], symbols: list[str]) -> dict[str, str]:
    """对 mapping 中未覆盖的 symbols，调用 stock_profile_cninfo 补充行业信息。"""
    uncovered = [s for s in symbols if s not in mapping]
    if not uncovered:
        return mapping

    print(f"[板块映射] CNINFO 补充 {len(uncovered)} 只未覆盖股票...")
    supplemented = 0
    for i, symbol in enumerate(uncovered):
        try:
            profile = ak.stock_profile_cninfo(symbol=symbol)
            if profile is not None and not profile.empty:
                industry = str(profile.iloc[0].get("所属行业", "")).strip()
                if industry and industry not in {"None", "nan", ""}:
                    mapping[symbol] = industry  # 保留CNINFO原始行业名
                    supplemented += 1
        except Exception:
            pass
        time.sleep(0.2)
        if (i + 1) % 20 == 0:
            print(f"[板块映射] CNINFO {i + 1}/{len(uncovered)}，补充 {supplemented} 只")

    print(f"[板块映射] CNINFO 补充完成：{supplemented}/{len(uncovered)} 只")
    return mapping


# ---------------------------------------------------------------------------
# 主入口：构建多数据源板块映射
# ---------------------------------------------------------------------------

def build_sector_mapping(
    cache_path: Path,
    supplement_symbols: list[str] | None = None,
) -> dict[str, str]:
    """构建股票代码→行业名称的映射（多数据源）。

    1. 优先读取缓存（7天有效）
    2. 缓存过期时用新浪行业重建基础映射
    3. 对 supplement_symbols 中未覆盖的股票，用 CNINFO 补充
    4. 合并后写入缓存
    """
    CACHE_DAYS = 7

    # 缓存有效则直接读取
    if cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
            if age <= CACHE_DAYS * 86400:
                mapping = _read_cache(cache_path)
                if mapping:
                    # 如果有补充需求，检查覆盖率
                    if supplement_symbols:
                        uncovered = [s for s in supplement_symbols if s not in mapping]
                        if uncovered:
                            ak = _try_import_akshare()
                            if ak is not None:
                                mapping = _supplement_with_cninfo(ak, mapping, uncovered)
                                _write_cache(cache_path, mapping)
                    return mapping
        except Exception:
            pass

    ak = _try_import_akshare()
    if ak is None:
        print("[板块映射] akshare 不可用，回退到缓存")
        return _read_cache(cache_path)

    # 新浪基础映射
    print("[板块映射] 构建新浪行业基础映射...")
    mapping = _build_sina_mapping(ak)
    print(f"[板块映射] 新浪基础映射：{len(mapping)} 只股票")

    # CNINFO 补充
    if supplement_symbols and mapping:
        mapping = _supplement_with_cninfo(ak, mapping, supplement_symbols)

    if mapping:
        _write_cache(cache_path, mapping)
        print(f"[板块映射] 已缓存 {len(mapping)} 只股票")
    else:
        print("[板块映射] 拉取结果为空，回退到缓存")
        return _read_cache(cache_path)

    return mapping


# ---------------------------------------------------------------------------
# 行业表现：同花顺（90行业）优先，新浪回退
# ---------------------------------------------------------------------------

def _fetch_ths_performance(ak) -> dict[str, dict]:
    """通过同花顺行业汇总获取90个行业的今日表现。"""
    try:
        df = ak.stock_board_industry_summary_ths()
    except Exception as exc:
        print(f"[板块表现] 同花顺行业汇总失败：{exc}")
        return {}

    if df is None or df.empty:
        return {}

    name_col = "板块" if "板块" in df.columns else df.columns[1]
    change_col = "涨跌幅" if "涨跌幅" in df.columns else None
    if change_col is None:
        for col in df.columns:
            if "涨跌幅" in str(col):
                change_col = col
                break
    if change_col is None:
        return {}

    items: list[tuple[str, float]] = []
    for _, row in df.iterrows():
        sector_name = str(row[name_col]).strip()  # 保留原始名
        change = _to_float(row[change_col])
        if sector_name:
            items.append((sector_name, change))

    total = len(items)
    items.sort(key=lambda x: x[1], reverse=True)

    perf: dict[str, dict] = {}
    for rank, (name, change) in enumerate(items, 1):
        perf[name] = {"change": change, "rank": rank, "total": total, "source": "ths"}
    print(f"[板块表现] 同花顺：{total} 个行业")
    return perf


def _fetch_sina_performance(ak) -> dict[str, dict]:
    """通过新浪行业获取49个板块的今日表现。"""
    try:
        spot_df = ak.stock_sector_spot(indicator="新浪行业")
    except Exception as exc:
        print(f"[板块表现] 新浪行业失败：{exc}")
        return {}

    if spot_df is None or spot_df.empty:
        return {}

    name_col = "板块" if "板块" in spot_df.columns else spot_df.columns[1]
    change_col = "涨跌幅" if "涨跌幅" in spot_df.columns else None
    if change_col is None:
        for col in spot_df.columns:
            if "涨跌幅" in str(col):
                change_col = col
                break
    if change_col is None:
        return {}

    items: list[tuple[str, float]] = []
    for _, row in spot_df.iterrows():
        sector_name = str(row[name_col]).strip()  # 保留原始名
        change = _to_float(row[change_col])
        if sector_name:
            items.append((sector_name, change))

    total = len(items)
    items.sort(key=lambda x: x[1], reverse=True)

    perf: dict[str, dict] = {}
    for rank, (name, change) in enumerate(items, 1):
        perf[name] = {"change": change, "rank": rank, "total": total, "source": "sina"}
    print(f"[板块表现] 新浪：{total} 个行业")
    return perf


def fetch_sector_performance() -> dict[str, dict]:
    """获取今日行业板块表现（同花顺+新浪合并，同花顺优先）。"""
    ak = _try_import_akshare()
    if ak is None:
        return {}

    # 同花顺（90行业）
    perf = _fetch_ths_performance(ak)

    # 新浪（49行业）补充：合并到同一dict，同花顺已有的不覆盖
    sina_perf = _fetch_sina_performance(ak)
    for name, data in sina_perf.items():
        if name not in perf:
            perf[name] = data
        # 也用标准化名做一次匹配
        normalized = _normalize_industry(name)
        if normalized not in perf:
            perf[normalized] = data

    print(f"[板块表现] 合并后共 {len(perf)} 个行业")
    return perf


# ---------------------------------------------------------------------------
# 个股板块信息查询
# ---------------------------------------------------------------------------

def get_sector_info(
    symbol: str,
    mapping: dict[str, str],
    performance: dict[str, dict],
) -> SectorInfo | None:
    """获取单只股票的板块信息。

    如果找不到返回 None。
    strength: rank在前30%为"强势"，后30%为"弱势"，中间为"中性"。
    """
    code = _pure_code(symbol)
    sector_name = mapping.get(code)
    if not sector_name:
        return None

    # 按优先级查找行业表现：原始名 → 标准化名
    perf = (
        performance.get(sector_name)
        or performance.get(_normalize_industry(sector_name))
    )
    if not perf:
        return None

    total = perf.get("total", 0)
    rank = perf.get("rank", 0)
    change = perf.get("change", 0.0)

    if total <= 0 or rank <= 0:
        return None

    top_threshold = max(1, int(total * 0.3))
    bottom_threshold = total - max(1, int(total * 0.3))

    if rank <= top_threshold:
        strength = "强势"
    elif rank > bottom_threshold:
        strength = "弱势"
    else:
        strength = "中性"

    return SectorInfo(
        sector_name=sector_name,
        sector_change=change,
        sector_rank=rank,
        sector_total_count=total,
        strength=strength,
    )


def sector_strength_score(sector_info: SectorInfo | None) -> int:
    """板块强度评分：强势+8 / 中性+4 / 弱势-3 / 无信息0。"""
    if sector_info is None:
        return 0
    if sector_info.strength == "强势":
        return 8
    if sector_info.strength == "中性":
        return 4
    if sector_info.strength == "弱势":
        return -3
    return 0
