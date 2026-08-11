# 选股公式落地说明

本文档整理上传的“大富翁”选股公式，并说明第一版在系统中的落地方式。

## 已识别公式

### BBI 及 MA60

原公式：

```text
MA23:MA(CLOSE,60);
BBIX:(MA(CLOSE,3)+MA(CLOSE,6)+MA(CLOSE,12)+MA(CLOSE,24))/4;
```

系统落地：

- 计算 `MA60`。
- 计算 `BBI=(MA3+MA6+MA12+MA24)/4`。
- 当 `CLOSE > BBI` 且 `CLOSE > MA60` 时，追加标签 `BBI/MA60`。

### 单针下20

原公式包含短期、中期、中长期、长期四条线：

```text
短期:100*(C-LLV(L,3))/(HHV(C,3)-LLV(L,3));
中期:100*(C-LLV(L,10))/(HHV(C,10)-LLV(L,10));
中长期:100*(C-LLV(L,21))/(HHV(C,21)-LLV(L,21));
长期:100*(C-LLV(L,31))/(HHV(C,31)-LLV(L,31));
```

系统落地的买点条件：

- 四线同时小于等于 `6`。
- 白线小于等于 `20` 且长期线大于等于 `70`。
- 白线向上穿越长期线，且长期线小于 `20`。
- 白线向上穿越中期线，且中期线小于 `30`。

任一条件命中，追加标签 `单针战法`。

### B1 买点

原公式核心：

```text
N:=9;
K:=SMA(RSV,3,1);
D:=SMA(K,3,1);
J:=3*K-2*D;
ZXDQ:=EMA(EMA(C,10),10);
ZXDKX:=(MA(C,M1)+MA(C,M2)+MA(C,M3)+MA(C,M4))/4;
XG: J<15 AND C>ZXDKX AND ZXDQ>ZXDKX;
```

系统落地：

- 计算 KDJ 的 `J` 值。
- 计算知行短期趋势线 `ZXDQ=EMA(EMA(C,10),10)`。
- 计算知行多空线 `ZXDKX=(MA14+MA28+MA57+MA114)/4`。
- 当 `J<15` 且 `C>ZXDKX` 且 `ZXDQ>ZXDKX` 时，追加标签 `B1/B2买点`。

### B2 买点

原公式核心：

```text
AFTER_B1:=B1_DAYS>0 AND B1_DAYS<=5;
ZF:=(C-REF(C,1))/REF(C,1)*100>4;
FANG:=V>REF(V,1);
J_COND:=J<55;
NO_UPPER:=(H-C)<=(C-L)*0.5 OR (H-C)<=(H-L)*0.3;
TREND_OK:=C>ZXDKX AND ZXDQ>ZXDKX;
XG: AFTER_B1 AND ZF AND FANG AND J_COND AND TREND_OK AND NO_UPPER;
```

系统落地：

- 最近 `1-5` 个交易日内出现过 B1。
- 当日涨幅大于 `4%`。
- 成交量大于上一日。
- `J<55`。
- 上影线不过长。
- 趋势仍满足 `C>ZXDKX` 且 `ZXDQ>ZXDKX`。

命中后追加标签 `B1/B2买点`。

### 砖型图反红

原公式核心：

```text
砖型图:=IF(VAR6A>4,VAR6A-4,0);
今天红柱:=砖型图 > REF(砖型图,1);
昨天绿柱:=REF(砖型图,1) < REF(砖型图,2);
红柱高度:=砖型图 - REF(砖型图,1);
绿柱高度:=REF(砖型图,2) - REF(砖型图,1);
高度达标:=红柱高度 >= 绿柱高度 * 2 / 3;
XG: 昨天绿柱 AND 今天红柱 AND 高度达标 AND 黄线达标;
```

系统落地：

- 昨天为绿柱。
- 今天转红柱。
- 红柱高度不低于上一段绿柱高度的 `2/3`。
- 同时满足知行多空线达标。

命中后追加标签 `砖形图`。

## 当前实现位置

- 公式计算模块：`src/trading_system/formula_indicators.py`
- 快照接入位置：`src/trading_system/data_normalizer.py`
- 标签输出位置：`data/market_snapshot.csv`

## 当前边界

- 公式已转成 Python 计算逻辑，但仍属于第一版落地。
- `白线`、`黄线`、`知行短期趋势线`、`知行多空线` 已可计算，但与原软件显示效果可能存在小幅口径差异。
- 上传文件里提到的 `3/4阴量线`、`活跃市值`、`增量资金` 只有标题，没有完整公式，暂未落地。
