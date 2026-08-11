# 基础架构说明

## 核心原则

本系统采用“先防守、再进攻”的架构。买点信号永远不是最高优先级，完整执行顺序为：

```text
样本库/规则库 → 行情快照 → 择时闸门 → 标的评分 → 买点候选 → 风控覆盖 → 交易计划
```

## 模块边界

### 真实行情适配层

真实行情不直接进入规则引擎，而是先经过三层适配：

- `data_provider.py`：负责外部数据源接入，当前支持 `csv` 验证源和可选 `akshare` 源。
- `market_breadth.py`：负责读取市场级择时指标，当前支持指南针活跃市值 CSV。
- `data_normalizer.py`：负责统一字段、计算 `volume_ma5`、`MACD`、白线、黄线、支撑位和压力位。
- `formula_indicators.py`：负责把上传的选股公式转成可自动命中的系统标签。
- `snapshot_store.py`：负责把标准化后的 `SecuritySnapshot` 保存为 `data/market_snapshot.csv`。

这层的目标是让外部 API 可以替换，但输入给交易引擎的数据结构保持稳定。

AkShare 真实行情采用独立配置 `config/data_source_akshare.json`，默认读取 `data/watchlist_real.csv` 并输出 `data/market_snapshot_akshare.csv`。为了避免外部接口波动导致整批中断，AkShare Provider 已加入重试、备用日线接口和逐股失败记录。

### 指南针活跃市值

指南针活跃市值作为市场级指标接入，不属于单只股票行情。系统通过 `data/compass_active_market_value.csv` 读取最新值，并写入每只股票快照的 `active_market_value_pct` 字段。

默认优先级：

```text
指南针活跃市值 CSV → 行情源代理值 → 报错或人工处理
```

这个字段会直接影响 `MarketTimingEngine` 的多头、震荡、空头判断，因此每天收盘后应先更新该文件，再运行 `update-market`。

### 样本库层

读取 `graphical-sample-library/data/samples.json`，用于：

- 构建战法标签词典。
- 统计样本数量与类型。
- 为后续图形识别模型提供训练标签。

### 规则层

`config/rulebook.json` 定义标签、信号方向和优先级。后续新增战法时，优先改配置，再补代码。

### 公式指标层

上传的选股公式已先落地为独立指标层，当前覆盖：

- `BBI/MA60`：用于判断中期趋势上下文。
- `单针下20`：命中后追加 `单针战法` 标签。
- `B1/B2`：命中后追加 `B1/B2买点` 标签。
- `砖型图反红`：命中后追加 `砖形图` 标签。
- `知行趋势达标`：命中后追加 `双线/白黄线` 标签。

公式层只负责识别和打标签，是否买入仍由 `TradePlanEngine` 结合择时和风控统一判断。

### 数据层

当前使用 CSV 快照，字段包括：

- `symbol`, `name`, `close`, `prev_close`
- `volume`, `volume_ma5`
- `active_market_value_pct`
- `macd_dif`, `macd_dea`
- `white_line`, `yellow_line`
- `support_price`, `resistance_price`
- `tags`

后续可替换为数据库、行情 API 或本地 Parquet。

### 引擎层

- `MarketTimingEngine`：判断多头、震荡、空头。
- `EntrySignalEngine`：根据行情字段与标签生成进攻信号。
- `RiskEngine`：根据止损/破线/S1/MACD 等生成防守动作。
- `TradePlanEngine`：合并择时、信号和风控，输出计划。

## 为什么先做离线

资料中多次强调“复盘计划”和“盘中无情执行”。所以第一版先做收盘后计划，而不是自动下单：

- 避免指标未复刻准确时误触发。
- 便于人工复核图形与战法。
- 后续可逐步接入提醒、回测、半自动执行。
