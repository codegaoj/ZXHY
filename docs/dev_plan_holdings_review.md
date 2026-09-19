# 实战系统优化开发计划：选股持股持续跟踪 + 复盘体系

## 现有基础

| 模块 | 状态 | 文件 |
|------|------|------|
| 持仓管理 UI | 已有 | trading_web_panel.py (持仓管理Tab) |
| 持仓引擎 | 已有 | portfolio_engine.py (check_holdings + _detect_signals) |
| SQLite 持久化 | 已有 | storage.py (holdings/transactions 表 + CRUD) |
| 7个持仓API | 已有 | trading_web_panel.py |
| 每日选股 | 已有 | screen_daily_candidates.py |
| 周期复盘 | 已有 | periodic_review.py (按周/月统计) |

## 总体架构

```
选股 → 手动买入(Web面板) → 每日收盘自动扫描持仓 → 生成卖出建议
  ↓                                                        ↓
候选股入库                                         交易记录增强(最大回撤/收益)
  ↓                                                        ↓
周期复盘 ← 策略归因分析 ← 单笔交易复盘 ← 手动卖出(Web面板)
  ↓
跨期对比看板
```

---

## Phase 1：每日收盘持仓复盘引擎（核心）

**目标**：每个交易日收盘后，自动扫描所有持仓，生成当日表现 + 卖出建议列表。

### 1.1 新建 `src/trading_system/daily_review_engine.py`

```python
class DailyReviewEngine:
    """每日收盘持仓复盘引擎"""

    def review_all_holdings(self) -> List[Dict]:
        """扫描所有 open 持仓，返回每只股票的复盘结果"""
        # 1. 从 DB 取所有 open holdings
        # 2. 逐只拉取最新K线（用 last_trading_day）
        # 3. 计算指标：
        #    - 当前价、市值、浮动盈亏、盈亏%
        #    - 持有天数
        #    - 买入后最大涨幅、最大回撤
        #    - 今日涨跌幅
        #    - 当前信号状态（破白线/破黄线/MACD死叉/止损/止盈）
        #    - 建议操作（持有/减仓/清仓）+ 原因
        # 4. 生成汇总：总持仓数、总市值、总盈亏、风险分布
        # 5. 存入 SQLite (daily_holdings_review 表)

    def _calc_max_return_drawdown(self, symbol, buy_date, buy_price) -> Dict:
        """计算买入后到今天的最大涨幅和最大回撤"""
        # 拉取 buy_date ~ last_trading_day 的日K线
        # max_high = K线最高价的最大值
        # max_return = (max_high - buy_price) / buy_price * 100
        # max_drawdown = (K线最低价最小值 - buy_price) / buy_price * 100

    def _generate_recommendation(self, signals, pnl_pct, holding_days) -> Dict:
        """根据信号+盈亏+持有天数生成操作建议"""
        # 优先级：
        #   止损（价格 < 买入价×0.95）→ 清仓
        #   破黄线 → 清仓或大幅减仓
        #   破白线 → 减仓
        #   MACD死叉 → 警告，关注
        #   止盈（价格 ≥ 阻力位）→ 可减仓锁利
        #   无信号 + 盈利 → 持有
        #   无信号 + 亏损 > 5% → 关注
        #   持有 > 10天 + 收益 < 2% → 考虑换股
```

### 1.2 新建 SQLite 表 `daily_holdings_reviews`

```sql
CREATE TABLE IF NOT EXISTS daily_holdings_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL,          -- 复盘日期（交易日）
    holding_id INTEGER NOT NULL,         -- 关联持仓ID
    symbol TEXT NOT NULL,
    name TEXT,
    current_price REAL,
    market_value REAL,
    floating_pnl REAL,
    floating_pnl_pct REAL,
    holding_days INTEGER,
    max_return_pct REAL,                -- 买入后最大涨幅
    max_drawdown_pct REAL,              -- 买入后最大回撤
    today_pct REAL,                     -- 今日涨跌幅
    broke_white INTEGER,                -- 是否破白线 0/1
    broke_yellow INTEGER,               -- 是否破黄线 0/1
    macd_death_cross INTEGER,            -- MACD是否死叉
    stop_loss_triggered INTEGER,        -- 是否触发止损
    take_profit_triggered INTEGER,     -- 是否触发止盈
    recommendation TEXT,                -- 建议操作
    recommendation_reason TEXT,         -- 建议原因
    risk_level TEXT,                    -- 风险等级 high/medium/low
    FOREIGN KEY (holding_id) REFERENCES holdings(id)
);
CREATE INDEX IF NOT EXISTS idx_dhr_date ON daily_holdings_reviews(review_date);
CREATE INDEX IF NOT EXISTS idx_dhr_symbol ON daily_holdings_reviews(symbol);
```

### 1.3 Web 面板增强

- 持仓管理 Tab 新增「今日复盘」按钮 + 结果卡片
- 每只持仓显示：当前价/盈亏%/信号/建议操作
- 风险等级颜色标记：红=清仓、橙=减仓、黄=关注、绿=持有
- 新增 API：`/holdings/daily-review`（GET，返回当日复盘结果）

### 1.4 集成到每日一键执行

在「每日一键执行」流程中加入持仓复盘步骤：
```
刷新行情 → 检查持仓（生成卖出建议） → 复盘昨日候选 → 生成今日候选
                 ↑ 新增：生成每日持仓复盘报告并存DB
```

**预估工作量**：2-3小时

---

## Phase 2：交易记录增强 + 单笔复盘

**目标**：卖出时自动记录完整的交易数据，支持单笔交易复盘分析。

### 2.1 扩展 `transactions` 表字段

新增列（ALTER TABLE 或重建）：
```sql
ALTER TABLE transactions ADD COLUMN buy_price REAL;           -- 买入价
ALTER TABLE transactions ADD COLUMN sell_price REAL;          -- 卖出价
ALTER TABLE transactions ADD COLUMN hold_days INTEGER;        -- 持有天数
ALTER TABLE transactions ADD COLUMN max_return_pct REAL;     -- 持有期最大涨幅
ALTER TABLE transactions ADD COLUMN max_drawdown_pct REAL;   -- 持有期最大回撤
ALTER TABLE transactions ADD COLUMN rr_ratio REAL;            -- 风险回报比
ALTER TABLE transactions ADD COLUMN exit_reason TEXT;        -- 退出原因（信号类型）
ALTER TABLE transactions ADD COLUMN market_regime TEXT;      -- 买入时市场状态
ALTER TABLE transactions ADD COLUMN entry_score REAL;        -- 买入时候选评分
```

### 2.2 增强 `portfolio_engine.py` 的 sell 逻辑

卖出时自动计算并记录：
- 从 K 线计算持有期最大涨幅、最大回撤
- R/R = (卖出价 - 买入价) / (买入价 - 止损价)
- 退出原因 = 触发的卖出信号类型
- 从 daily_holdings_reviews 取最近一次复盘的信号状态

### 2.3 单笔交易复盘 UI

- 持仓管理 Tab 下方新增「交易历史」区域（已有 /transactions API）
- 每笔已平仓交易显示：买入日期/价格、卖出日期/价格、盈亏/%、持有天数、最大涨幅、最大回撤、退出原因
- 支持按盈亏排序、按时间筛选
- 新增 API：`/transactions/closed`（GET，只返回已平仓交易）

**预估工作量**：2小时

---

## Phase 3：策略归因分析

**目标**：按信号类型/行业/评分/市场状态维度做归因，找出赚钱和亏钱的策略。

### 3.1 新建 `src/trading_system/attribution_engine.py`

```python
class AttributionEngine:
    """策略归因分析引擎"""

    def analyze(self, transactions: List[Dict]) -> Dict:
        """对已平仓交易做多维度归因"""

    def by_signal_type(self, txns) -> Dict:
        """按买入信号类型（B1/B2/砖形图/滴滴/双线等）归因"""
        # 从 entry_score 对应的候选记录取 tags

    def by_sector(self, txns) -> Dict:
        """按行业归因"""

    def by_score_range(self, txns) -> Dict:
        """按候选评分区间归因（<30/30-40/40-50/>50）"""

    def by_market_regime(self, txns) -> Dict:
        """按买入时市场状态归因（牛市/震荡偏强/中性/偏弱/熊市）"""

    def by_holding_days(self, txns) -> Dict:
        """按持有天数归因（1-3天/4-7天/8-15天/>15天）"""

    def by_exit_reason(self, txns) -> Dict:
        """按退出原因归因（止损/破白线/破黄线/止盈/主动换股）"""

    def generate_report(self, analysis: Dict) -> str:
        """生成 HTML 归因报告"""
```

### 3.2 Web 面板新增「策略归因」区域

- 报告与历史 Tab 新增「策略归因分析」卡片
- 展示各维度的胜率、平均收益、盈亏比
- 标注最优策略和最差策略
- 新增 API：`/attribution`（GET，返回归因分析结果）

**预估工作量**：2-3小时

---

## Phase 4：跨期对比看板

**目标**：跟踪周/月复盘指标的变化趋势，发现策略退化或改善。

### 4.1 扩展 `periodic_review.py`

- 新增 `--compare` 参数，生成跨期对比报告
- 从 SQLite 读取历史 periodic_review 记录
- 计算环比变化：胜率变化、收益变化、行业轮动
- 生成趋势图（HTML 内嵌 SVG 折线图）

### 4.2 跨期对比指标

| 指标 | 展示方式 |
|------|----------|
| 胜率趋势 | 折线图，按周/月 |
| 平均收益趋势 | 折线图 |
| 最大盈利/亏损 | 柱状图对比 |
| 行业胜率热力图 | 矩阵：行业 × 周期 |
| 信号胜率变化 | 折线图，按信号类型 |
| 破线率趋势 | 折线图 |

### 4.3 Web 面板新增「跨期对比」区域

- 报告与历史 Tab 新增「跨期趋势」卡片
- 可选时间范围：近4周/近3月/近6月
- 新增 API：`/reports/trend`（GET，返回趋势数据）

**预估工作量**：2小时

---

## 实施顺序

| 阶段 | 内容 | 预估时间 | 依赖 |
|------|------|----------|------|
| Phase 1 | 每日收盘持仓复盘引擎 | 2-3h | 无 |
| Phase 2 | 交易记录增强 + 单笔复盘 | 2h | Phase 1 |
| Phase 3 | 策略归因分析 | 2-3h | Phase 2 |
| Phase 4 | 跨期对比看板 | 2h | Phase 2+3 |

**总计**：8-10小时，可分2-3次完成。

## 文件变更清单

| 文件 | 操作 | 阶段 |
|------|------|------|
| `src/trading_system/daily_review_engine.py` | 新建 | P1 |
| `src/trading_system/attribution_engine.py` | 新建 | P3 |
| `src/trading_system/storage.py` | 修改（加表+字段） | P1+P2 |
| `src/trading_system/portfolio_engine.py` | 修改（增强sell逻辑） | P2 |
| `tools/trading_web_panel.py` | 修改（UI+API） | P1-P4 |
| `tools/periodic_review.py` | 修改（跨期对比） | P4 |
