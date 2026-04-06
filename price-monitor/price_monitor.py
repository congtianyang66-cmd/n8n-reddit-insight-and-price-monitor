"""
Mugler Duty-Free Price Monitor (single CSV version)

作用：
1. 抓取 Boston Duty Free 指定 SKU：
   - Mugler Angel EDP 100ml
   - Mugler Alien EDP 90ml
2. 解决多规格商品页误抓默认规格的问题，按 target_size 精准匹配
3. 每次运行仅追加到一个历史数据文件：data/dutyfree_price_history.csv
4. 不额外生成 daily summary / latest change 等分析 CSV
5. 终端只打印本轮抓取结果与历史表最后几行，便于后续接 n8n

可用于面试讲述的尝试过程：
- 第一版直接抓整页第一个价格，误抓到了默认规格（50ml / 60ml）。
- 之后检查 HTML 发现这是多规格共页展示。
- 最终改为从 HTML 中提取带 size / original_price / final_price 的变体信息，
  再按目标规格精准匹配。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT_SECONDS = 20

PRODUCTS_TO_MONITOR = [
    {
        "product_name": "Mugler Angel EDP",
        "url": "https://boston.shopdutyfree.com/76/mugler-angel-100ml-eau-de-parfum",
        "target_size": "100ml",
        "channel": "duty_free",
        "market": "Boston",
    },
    {
        "product_name": "Mugler Alien EDP",
        "url": "https://boston.shopdutyfree.com/76/mugler-alien-90ml-eau-de-parfum",
        "target_size": "90ml",
        "channel": "duty_free",
        "market": "Boston",
    },
]

BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "dutyfree_price_history.csv")


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def extract_variants_from_html(html: str) -> List[Dict[str, Optional[str]]]:
    """
    这里保留 notebook 中验证通过的版本。
    说明：
    - 这个正则不是最优雅的 JSON 解析方式，但在当前页面结构下能工作。
    - 它依赖页面脚本里嵌入的变体对象，再从中抓 size / original_price / final_price。
    """
    variants: List[Dict[str, Optional[str]]] = []
    pattern = re.compile(r'"(?P<item_key>\d+)":\{(?P<body>.*?)\}', re.S)

    for m in pattern.finditer(html):
        block = m.group(0)

        if '"size":"' not in block or '"original_price":"' not in block or '"final_price":"' not in block:
            continue

        def grab(field: str) -> Optional[str]:
            mm = re.search(rf'"{field}":"(.*?)"', block)
            return mm.group(1) if mm else None

        variant = {
            "item_key": m.group("item_key"),
            "id": grab("id"),
            "item_id": grab("item_id"),
            "name": grab("name"),
            "brand": grab("brand"),
            "size": grab("size"),
            "original_price": grab("original_price"),
            "final_price": grab("final_price"),
            "currency_code": grab("currency_code"),
            "currency_symbol": grab("currency_symbol"),
            "is_promotion": grab("is_promotion"),
            "promo_text": grab("promo_text"),
            "concentration": grab("concentration"),
            "code_internal": grab("code_internal"),
        }
        variants.append(variant)

    return variants


def parse_duty_free_variant(
    html: str,
    product_name: str,
    url: str,
    target_size: str,
    market: str = "Boston",
    channel: str = "duty_free",
) -> Dict[str, Any]:
    variants = extract_variants_from_html(html)
    target_size_normalized = target_size.lower().strip()

    matched = None
    for v in variants:
        size_value = v.get("size")
        if size_value and size_value.lower().strip() == target_size_normalized:
            matched = v
            break

    if not matched:
        return {
            "capture_date": datetime.now().strftime("%Y-%m-%d"),
            "product_name": product_name,
            "channel": channel,
            "market": market,
            "target_size": target_size,
            "currency": None,
            "list_price": None,
            "sale_price": None,
            "promo_pct": None,
            "promo_text": None,
            "status": "target_size_not_found",
            "source_url": url,
        }

    list_price = float(matched["original_price"]) if matched.get("original_price") else None
    sale_price = float(matched["final_price"]) if matched.get("final_price") else None

    promo_pct = None
    if list_price is not None and sale_price is not None and list_price > 0:
        promo_pct = round((list_price - sale_price) / list_price * 100, 2)

    return {
        "capture_date": datetime.now().strftime("%Y-%m-%d"),
        "product_name": product_name,
        "channel": channel,
        "market": market,
        "target_size": target_size,
        "currency": matched.get("currency_code") or "USD",
        "list_price": list_price,
        "sale_price": sale_price,
        "promo_pct": promo_pct,
        "promo_text": matched.get("promo_text"),
        "status": "ok",
        "source_url": url,
    }


def run_current_capture(products: List[Dict[str, str]]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for product in products:
        html = fetch_html(product["url"])
        row = parse_duty_free_variant(
            html=html,
            product_name=product["product_name"],
            url=product["url"],
            target_size=product["target_size"],
            market=product.get("market", "Boston"),
            channel=product.get("channel", "duty_free"),
        )
        rows.append(row)
    return pd.DataFrame(rows)


def append_to_history(df_current_raw: pd.DataFrame, history_file: str = HISTORY_FILE) -> tuple[pd.DataFrame, pd.DataFrame]:
    os.makedirs(os.path.dirname(history_file), exist_ok=True)

    now = datetime.now()
    capture_date = now.strftime("%Y-%m-%d")
    capture_time = now.strftime("%H:%M:%S")
    run_id = now.strftime("%Y%m%d_%H%M%S")

    if os.path.exists(history_file):
        df_history = pd.read_csv(history_file)
        if "capture_date" in df_history.columns:
            df_history["capture_date"] = df_history["capture_date"].astype(str)
            today_runs = df_history[df_history["capture_date"] == capture_date]
            if len(today_runs) > 0 and "run_seq" in df_history.columns:
                run_seq = int(today_runs["run_seq"].max()) + 1
            else:
                run_seq = 1
        else:
            run_seq = 1
    else:
        df_history = pd.DataFrame()
        run_seq = 1

    df_current = df_current_raw.copy()
    for col in ["capture_date", "capture_time", "run_id", "run_seq", "data_source"]:
        if col in df_current.columns:
            df_current = df_current.drop(columns=[col])

    df_current["capture_date"] = capture_date
    df_current["capture_time"] = capture_time
    df_current["run_id"] = run_id
    df_current["run_seq"] = run_seq
    df_current["data_source"] = "python_script_run"

    if df_history.empty:
        df_all = df_current.copy()
    else:
        all_columns = list(dict.fromkeys(df_history.columns.tolist() + df_current.columns.tolist()))
        df_history = df_history.reindex(columns=all_columns)
        df_current = df_current.reindex(columns=all_columns)
        df_all = pd.concat([df_history, df_current], ignore_index=True)

    df_all.to_csv(history_file, index=False)
    return df_current, df_all


def print_run_summary(df_current: pd.DataFrame, df_all: pd.DataFrame) -> None:
    print("=" * 80)
    print("本轮抓取结果")
    print("=" * 80)
    print(df_current.to_string(index=False))

    print("\n" + "=" * 80)
    print(f"历史表总行数：{len(df_all)}")
    print("历史表最后 10 行")
    print("=" * 80)
    print(df_all.tail(10).to_string(index=False))


def main() -> None:
    df_current_raw = run_current_capture(PRODUCTS_TO_MONITOR)
    df_current, df_all = append_to_history(df_current_raw, HISTORY_FILE)
    print_run_summary(df_current, df_all)


if __name__ == "__main__":
    main()
