# A股交易复盘

**A股交易复盘**：把券商交割单或手工成交记录整理成持仓周期，按真实费率计算净盈亏，输出胜率、盈亏比、期望、费用结构和最大回撤，并检测处置效应、亏损加仓、过度交易等行为偏差。

## 安装

```bash
git clone https://github.com/LINZIQI24GITHUB/finansi-a-share-review.git
```

放到 Agent 的 skills 目录：

```text
Claude Code   ~/.claude/skills/finansi-a-share-review/
Codex         ~/.codex/skills/finansi-a-share-review/
Cursor        ~/.cursor/skills/finansi-a-share-review/
通用           ~/.agents/skills/finansi-a-share-review/
```

只用 Python 标准库，不需要安装第三方包。

## 快速开始

```bash
# 导入券商交割单
python scripts/import_broker.py --input examples/broker_sample.csv --output fills.csv --append

# 查看统计
python scripts/review.py --file fills.csv stats

# 查看持仓周期
python scripts/review.py --file fills.csv list --cycles

# 导出复盘报告
python scripts/review.py --file fills.csv report --out 复盘.md
```

也可以直接对 Agent 说：

```text
用 finansi-a-share-review 复盘这个月的交割单，告诉我胜率、盈亏比和最大的行为问题。
```

## 输出

- 成交笔数、标的数、已平仓/未平仓
- 已实现盈亏、胜率、盈亏比、期望
- 盈利因子、最大回撤
- 佣金、印花税、过户费拆解
- 盈利单/亏损单持有天数
- 按策略、按月的盈亏
- 处置效应、亏损加仓、过度交易提示

## 数据边界

- 成交记录保存在本地 CSV；不上传、不联网。
- 示例 `examples/demo_fills.csv` 是合成数据，不是真实账户。
- 券商交割单字段不统一时，先跑 `--dry-run` 预览再导入。
- 复盘结果是历史归因，不构成投资建议。

## 边界

- 不做自动下单、账户托管、实时行情。
- 不分析基金净值、期货、期权。
- 行为偏差提示是规则触发，不是心理诊断。

## License

MIT. See [LICENSE](LICENSE).
