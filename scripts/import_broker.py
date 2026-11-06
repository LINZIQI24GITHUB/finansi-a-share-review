#!/usr/bin/env python3
"""把券商导出的成交/交割单 CSV 转成 journal 的 fills.csv。

用法:
    python ashare_import.py --input 交割单.csv --output fills.csv
    python ashare_import.py --input 交割单.csv --dry-run      # 只预览不写入
    python ashare_import.py --input 交割单.csv --append       # 追加并去重

支持的列名（自动识别，同花顺/东方财富/华泰/中信等常见导出）:
    成交日期 / 日期 / 交易日期
    成交时间 / 时间
    证券代码 / 股票代码 / 代码
    证券名称 / 股票名称 / 名称
    操作 / 买卖标志 / 买卖方向 / 委托方向 / 业务名称
    成交价格 / 成交均价 / 价格
    成交数量 / 数量 / 成交量
    佣金 / 手续费、印花税、过户费（缺失时按规则重算）

编码自动尝试 utf-8-sig / gbk / gb18030。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compat import calc_fees  # noqa: E402

FIELDS = [
    "date", "time", "code", "name", "side", "price", "qty",
    "commission", "stamp_duty", "transfer_fee", "strategy", "tag", "note",
]

ALIASES = {
    "date": ["成交日期", "日期", "交易日期", "发生日期", "委托日期"],
    "time": ["成交时间", "时间", "委托时间"],
    "code": ["证券代码", "股票代码", "代码", "合约代码"],
    "name": ["证券名称", "股票名称", "名称", "证券简称"],
    "side": ["操作", "买卖标志", "买卖方向", "委托方向", "业务名称", "摘要"],
    "price": ["成交价格", "成交均价", "价格", "成交价"],
    "qty": ["成交数量", "数量", "成交量", "股数"],
    "commission": ["佣金", "手续费", "交易手续费", "净佣金"],
    "stamp_duty": ["印花税"],
    "transfer_fee": ["过户费", "经手费", "规费"],
}

BUY_WORDS = ("买", "证券买入", "担保品买入", "融资买入", "申购")
SELL_WORDS = ("卖", "证券卖出", "担保品卖出", "融券卖出", "赎回")


def read_rows(path: Path) -> list[dict]:
    for enc in ("utf-8-sig", "gbk", "gb18030", "utf-8"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"✗ 无法解码 {path}，请另存为 UTF-8 或 GBK 的 CSV")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0]
    delim = max([",", "\t", ";"], key=header.count)
    return list(csv.DictReader(lines, delimiter=delim))


def pick(row: dict, key: str) -> str:
    for alias in ALIASES[key]:
        for col, val in row.items():
            if col and col.strip() == alias:
                return (val or "").strip()
    return ""


def norm_side(raw: str) -> str | None:
    raw = raw or ""
    if any(w in raw for w in BUY_WORDS):
        return "buy"
    if any(w in raw for w in SELL_WORDS):
        return "sell"
    return None


def norm_date(raw: str) -> str:
    raw = raw.strip().replace("/", "-").replace(".", "-")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def convert(rows: list[dict]) -> tuple[list[dict], list[str]]:
    out, skipped = [], []
    for i, row in enumerate(rows, 2):
        side = norm_side(pick(row, "side"))
        code = pick(row, "code").strip()
        price = pick(row, "price")
        qty = pick(row, "qty")
        if not (side and code and price and qty):
            skipped.append(f"第{i}行 缺关键字段（方向/代码/价格/数量），跳过")
            continue
        try:
            amount = float(price) * abs(int(float(qty)))
        except ValueError:
            skipped.append(f"第{i}行 价格或数量无法解析，跳过")
            continue
        fee_raw = pick(row, "commission")
        stamp_raw = pick(row, "stamp_duty")
        transfer_raw = pick(row, "transfer_fee")
        auto = calc_fees(amount, side)
        out.append({
            "date": norm_date(pick(row, "date")),
            "time": pick(row, "time") or "",
            "code": code.zfill(6),
            "name": pick(row, "name"),
            "side": side,
            "price": f"{float(price):.3f}".rstrip("0").rstrip("."),
            "qty": str(abs(int(float(qty)))),
            "commission": f"{abs(float(fee_raw)):.2f}" if fee_raw else f"{auto.commission:.2f}",
            "stamp_duty": f"{abs(float(stamp_raw)):.2f}" if stamp_raw else f"{auto.stamp_duty:.2f}",
            "transfer_fee": f"{abs(float(transfer_raw)):.2f}" if transfer_raw else f"{auto.transfer_fee:.2f}",
            "strategy": "", "tag": "", "note": "导入",
        })
    return out, skipped


def key(r: dict) -> tuple:
    return (r["date"], r["code"], r["side"], r["qty"], r["price"])


def main() -> None:
    ap = argparse.ArgumentParser(description="券商成交单 → A股交易日志 fills.csv")
    ap.add_argument("--input", required=True, help="券商导出的 CSV")
    ap.add_argument("--output", default="fills.csv")
    ap.add_argument("--append", action="store_true", help="追加到已有文件并去重")
    ap.add_argument("--dry-run", action="store_true", help="只预览")
    args = ap.parse_args()

    raw = read_rows(Path(args.input))
    fills, skipped = convert(raw)
    fills.sort(key=lambda r: (r["date"], r["time"] or ""))

    print(f"解析 {len(raw)} 行 → 有效成交 {len(fills)} 笔")
    for s in skipped[:8]:
        print("  !", s)
    if skipped[8:]:
        print(f"  ! 另有 {len(skipped) - 8} 行被跳过")

    if args.dry_run:
        print("\n预览（前 5 笔）：")
        for f in fills[:5]:
            print(f"  {f['date']} {f['code']} {f['name']} {f['side']} "
                  f"{f['qty']}股 @ {f['price']} 佣金{f['commission']}")
        return

    out = Path(args.output)
    existing: list[dict] = []
    if args.append and out.exists():
        with out.open("r", encoding="utf-8-sig", newline="") as fh:
            existing = [{k: (v or "") for k, v in row.items()} for row in csv.DictReader(fh)]
    seen = {key(r) for r in existing}
    merged = existing + [f for f in fills if key(f) not in seen]
    merged.sort(key=lambda r: (r["date"], r.get("time") or ""))
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(merged)
    print(f"\n✓ 写入 {out.resolve()}，共 {len(merged)} 笔"
          f"（新增 {len(merged) - len(existing)}，去重跳过 {len(fills) - (len(merged) - len(existing))}）")
    print("  下一步：python ashare_journal.py stats")


if __name__ == "__main__":
    main()
