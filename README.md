# 股票交易系统基础架构

这是基于已整理的战法资料与图形样本库搭建的第一版交易系统骨架。它先解决四件事：

1. 读取图形样本库，形成可检索的战法标签索引。
2. 建立统一的数据模型：市场状态、标的快照、持仓、信号、交易计划。
3. 实现基础规则引擎：择时闸门、买点识别、风控优先级、仓位建议。
4. 提供命令行入口，便于后续接入真实行情源、回测、看板和自动提醒。

> 当前版本只用于系统设计、复盘和规则验证，不构成投资建议，也不连接实盘下单。

## 目录结构

```text
trading-system-core/
├── config/
│   ├── rulebook.json              # 战法标签与规则优先级配置
│   ├── sample_paths.json          # 图形样本库路径配置
│   ├── data_source.json           # 离线/默认数据源配置
│   └── data_source_akshare.json   # AkShare 真实行情配置
├── data/
│   ├── demo_market_snapshot.csv   # 离线演示行情快照
│   ├── watchlist.csv              # 自选股池
│   ├── watchlist_real.csv         # 真实股票池示例
│   └── compass_active_market_value.csv # 指南针活跃市值导入文件
├── docs/
│   ├── architecture.md            # 架构说明
│   └── stock_selection_formulas.md # 选股公式落地说明
├── outputs/                       # 运行输出目录
├── src/
│   └── trading_system/
│       ├── cli.py                 # 命令行入口
│       ├── models.py              # 核心数据模型
│       ├── sample_library.py      # 样本库读取与标签索引
│       ├── data_provider.py       # 真实行情数据源适配
│       ├── data_normalizer.py     # 行情字段标准化和指标计算
│       ├── formula_indicators.py  # 上传选股公式的 Python 落地
│       ├── snapshot_store.py      # 快照 CSV 读写
│       ├── rulebook.py            # 规则配置加载
│       ├── indicators.py          # 基础指标计算
│       ├── engines.py             # 择时/买点/风控/计划引擎
│       └── reporting.py           # 输出报告
└── tests/
    └── smoke_test.py              # 基础冒烟验证
```

## 快速开始

在项目目录运行：

```powershell
python -m src.trading_system.cli samples-summary
python -m src.trading_system.cli run-demo
python -m src.trading_system.cli update-market
python -m src.trading_system.cli run-live-plan
python -m src.trading_system.cli validate-akshare --config config/data_source_akshare.json --limit 1
python tests\smoke_test.py
```

运行后会在 `outputs/` 下生成：

- `sample_tag_summary.json`：图形样本库标签统计
- `demo_trade_plan.json`：演示交易计划
- `demo_trade_plan.md`：便于阅读的交易计划摘要
- `live_trade_plan.json`：真实行情/准真实行情交易计划
- `live_trade_plan.md`：真实行情/准真实行情交易计划摘要

## 真实行情适配层

第一版真实行情适配层采用可替换的数据源设计：

```text
外部行情源 → DataProvider → DataNormalizer → market_snapshot.csv → TradePlanEngine
```

默认配置在 `config/data_source.json`：

- `source: "csv"`：使用 `data/demo_market_snapshot.csv` 生成准真实行情快照，适合离线验证。
- `source: "akshare"`：使用 AkShare 拉取 A 股日线行情，需要先安装 `akshare`。
- `watchlist_path`：自选股池，默认 `data/watchlist.csv`。
- `market_snapshot_path`：真实行情快照输出，默认 `data/market_snapshot.csv`。
- `market_breadth_source: "compass_csv"`：优先使用指南针活跃市值 CSV 作为择时闸门输入。
- `compass_active_market_value_path`：指南针活跃市值导入文件，默认 `data/compass_active_market_value.csv`。

指南针活跃市值文件格式：

```csv
date,active_market_value,active_market_value_pct,source,note
2026-08-09,4.82,4.82,compass,指南针活跃市值
```

每天收盘后，把指南针里的活跃市值更新到 `active_market_value_pct`。运行 `update-market` 时，系统会优先读取这个值；如果文件缺失或字段异常，会回退到行情源代理值。

安装 AkShare：

```powershell
pip install akshare
```

建议保留 `config/data_source.json` 作为离线回归配置，真实行情使用独立的 `config/data_source_akshare.json`。自选股池格式：

```csv
symbol,name,tags
000001,平安银行,B1/B2买点;关键K
600519,贵州茅台,板块/主线;MACD
```

默认离线模式每天收盘后执行：

```powershell
python -m src.trading_system.cli update-market
python -m src.trading_system.cli run-live-plan
```

## AkShare 真实股票池

AkShare 配置文件为 `config/data_source_akshare.json`，真实股票池示例为 `data/watchlist_real.csv`。

第一次使用先验证 AkShare 是否可拉取真实行情：

```powershell
python -m src.trading_system.cli validate-akshare --config config/data_source_akshare.json --limit 1 --days 90
```

验证通过后，生成真实股票池行情快照：

```powershell
python -m src.trading_system.cli update-market --config config/data_source_akshare.json
```

再生成交易计划：

```powershell
python -m src.trading_system.cli run-live-plan --config config/data_source_akshare.json
```

AkShare 输出快照默认为 `data/market_snapshot_akshare.csv`。如果个别股票接口临时失败，系统会继续处理成功股票，并把失败明细写入 `outputs/market_update_failures.json`。

## 历史回测与收盘报告

回测 B1、S1、滴滴、砖形图四类规则：

```powershell
python -m src.trading_system.cli backtest-rules --config config/data_source_akshare.json --limit 2 --days 180 --holding-days 5
```

运行后输出：

- `outputs/backtest_rules.json`
- `outputs/backtest_rules.md`

生成每日收盘 HTML 报告：

```powershell
python -m src.trading_system.cli daily-report --config config/data_source_akshare.json
```

运行后输出：

- `outputs/daily_close_report.html`

## 第一版规则边界

系统当前将资料中的交易思想拆成可扩展模块：

- `择时闸门`：活跃市值、MACD 零轴、多空状态决定是否允许进攻。
- `买点引擎`：B1/B2、关键K、量价关系、双线/黄白线、砖形图、单针战法等标签映射为信号候选。
- `公式指标层`：把 BBI/MA60、单针下20、B1/B2、砖型图反红等选股公式转成可自动命中的标签。
- `风控引擎`：没涨/盈转亏、止损、放飞、S1/卖点、滴滴战法、破白线/黄线、MACD 否决优先于买点。
- `组合层`：按风险等级输出建议仓位，不直接下单。

## 下一步建议

1. 复刻或导入白线、黄线、红线、砖形图等专属指标的最终口径。
2. 将回测从“信号命中统计”升级为“资金曲线、最大回撤、胜率/赔率”。
3. 把每日收盘报告升级为 Web 看板，增加筛选、图表和历史对比。
4. 接入持仓文件，生成“已有持仓 + 自选股池”的统一交易计划。
