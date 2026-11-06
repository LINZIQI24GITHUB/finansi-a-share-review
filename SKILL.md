---
name: finansi-a-share-review
description: A股交易复盘。把券商交割单或手工成交记录整理成持仓周期，按真实费率算净盈亏，输出胜率、盈亏比、期望、费用结构与最大回撤，并检测处置效应、亏损加仓、过度交易等行为偏差。当用户提到复盘、交割单、交易记录、胜率、盈亏比、这个月做得怎么样、买卖点回顾时使用。
---

# A 股复盘（finansi-a-share-review）

把「感觉这月还行」变成可核对的数字。链路只有三步：**成交 → 持仓周期 → 指标与行为诊断**。

## 快速开始

> 帮我复盘这个月，交割单在 `examples/broker_sample.csv`

```bash
python scripts/import_broker.py --input examples/broker_sample.csv --output fills.csv --append
python scripts/review.py --file fills.csv stats
```

## 什么时候用我

- 「这个月做得怎么样，胜率和盈亏比多少」
- 「交割单怎么导进来」
- 「我是不是拿不住盈利 / 老在亏损时加仓」
- 「手续费一年吃掉多少」

## 什么时候别用我

- 想回测某个策略 → `a-share-backtest`
- 想看组合风险（相关性、集中度）→ `a-share-portfolio`

## 输入契约

| 需要 | 必填 | 说明 |
|---|---|---|
| 交割单 CSV | ✓ | 券商导出格式，列名自动识别（成交日期/证券代码/操作/成交价格…） |
| 或手工成交 CSV | ✓ | `date,code,side,price,qty`，其余字段可留空自动算 |
| 起始资金 | | 只用于费用占比，默认 10 万 |

## 命令

```bash
python scripts/import_broker.py --input 交割单.csv --dry-run          # 先预览
python scripts/import_broker.py --input 交割单.csv --output fills.csv --append
python scripts/review.py --file fills.csv add --code 600519 --side buy --price 1680 --qty 100
python scripts/review.py --file fills.csv list --cycles
python scripts/review.py --file fills.csv stats
python scripts/review.py --file fills.csv report --out 复盘.md
python scripts/review.py --file examples/demo_fills.csv demo           # 离线自检
```

## 真实输出

```
  成交笔数 13     标的 6    已平仓 5    未平仓 1
  已实现盈亏      6,415.59 元      胜率 60.00%
  平均盈利       4,100.66 元      平均亏损  -2,943.19 元
  盈亏比             1.39          期望/笔  1,283.12 元
  盈利因子           2.09          最大回撤  4,449.70 元
  盈利单持有        10 交易日   亏损单持有 26 交易日
  费用：佣金 199.71 + 印花税 189.30 + 过户费 8.01 = 397.02 元
```

## 指标口径

- 成本用**移动平均（摊薄成本）**，与券商 App 显示一致
- 盈亏是**净额**：已扣买入佣金、卖出佣金、印花税、过户费
- 胜率/盈亏比只统计**已平仓周期**，未平仓的不算
- 持有天数按交易日近似（剔除周末，不含法定节假日）

## 行为诊断（自动触发）

| 标签 | 触发条件 |
|---|---|
| 处置效应 | 盈利单持有 < 亏损单持有的 70% |
| 亏损加仓 | 跌破持仓均价后继续买入 |
| 过度交易 | 周均成交 > 10 笔 |
| 过度集中 | 单票买入额 > 总买入额 50% |
| 费用偏高 | 总费用 > 买入额 1% |

## 失败与降级

| 现象 | 原因 | 处理 |
|---|---|---|
| `解析 0 笔` | 表头不是券商格式 | 检查列名，或用 `add` 手工录 |
| `卖出超过持仓` | 漏录了买入 | 补录后再统计 |
| `校验：卖出未计印花税` | 手工填了 0 | 留空让它自动算 |

## 评测卡

| 项 | 判定标准 |
|---|---|
| 费率正确 | 佣金万 2.5（最低 5 元）+ 卖出印花税 0.05% + 过户费 |
| 周期聚合 | 分批买卖不被拆成多笔，按清仓计一个周期 |
| 行为提示 | 至少给出 1 条可解释的行为诊断，而不是只报数字 |
| 可复现 | 同一份交割单两次统计结果一致 |
| 自检 | `demo` 通过 |

## 已知局限

- 分红送转、可转债转股、融资融券利息不计入盈亏，需在 `note` 注明
- 不支持期货、期权、港股通
- 持有天数为交易日近似值
