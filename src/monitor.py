from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"

SEAGM_URL = "https://www.seagm.com/zh-cn/itunes-gift-card-united-states"
SEAGM_SETTINGS_URL = (
    "https://www.seagm.com/zh-cn/language_currency"
    "?origin=%2Fzh-cn%2Fitunes-gift-card-united-states"
)
FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD&to=CNY"
ER_API_URL = "https://open.er-api.com/v6/latest/USD"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_html() -> str:
    session = requests.Session()

    settings_response = session.get(SEAGM_SETTINGS_URL, headers=HEADERS, timeout=30)
    settings_response.raise_for_status()
    settings_soup = BeautifulSoup(settings_response.text, "html.parser")
    settings_form = settings_soup.select_one("#lang_currency_settings form")
    if settings_form is None or not settings_form.get("action"):
        raise RuntimeError("SEAGM 货币设置页面异常，无法切换为美元")

    settings_data = {
        node["name"]: node.get("value", "")
        for node in settings_form.select("input[name]")
    }
    settings_data.update({"language": "zh", "currency": "USD"})
    settings_headers = {
        **HEADERS,
        "Origin": "https://www.seagm.com",
        "Referer": SEAGM_SETTINGS_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    settings_submit = session.post(
        urljoin(settings_response.url, str(settings_form["action"])),
        headers=settings_headers,
        data=settings_data,
        timeout=30,
    )
    settings_submit.raise_for_status()

    response = session.get(SEAGM_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    if "iTunes Gift Card" not in response.text:
        raise RuntimeError("SEAGM 页面返回内容异常，未找到礼品卡标识")

    soup = BeautifulSoup(response.text, "html.parser")
    currency_node = soup.select_one(".language_currency .currency")
    if currency_node is None or currency_node.get_text(strip=True).upper() != "USD":
        currency = currency_node.get_text(strip=True) if currency_node else "未知"
        raise RuntimeError(f"SEAGM 结算货币异常，预期 USD，实际为 {currency}")
    return response.text


def parse_products(html: str) -> list[dict[str, float | int | str]]:
    """从美国区商品页的 SKU 列表提取面额、原价和折后售价。"""
    soup = BeautifulSoup(html, "html.parser")
    products: dict[float, dict[str, float | int | str]] = {}

    for sku in soup.select("#cardType .SKU_type"):
        name_node = sku.select_one(".sku span")
        original_node = sku.select_one(".price_origional")
        sale_node = sku.select_one(".price_discount") or original_node
        if not name_node or not original_node or not sale_node:
            continue

        name = name_node.get_text(" ", strip=True)
        face_match = re.search(r"([\d,.]+)\s*(?:USD|美金)", name, re.IGNORECASE)
        original_match = re.search(r"US\$\s*([\d,.]+)", original_node.get_text(" ", strip=True))
        sale_match = re.search(r"US\$\s*([\d,.]+)", sale_node.get_text(" ", strip=True))
        if not face_match or not original_match or not sale_match:
            continue

        face_value = float(face_match.group(1).replace(",", ""))
        list_price_usd = float(original_match.group(1).replace(",", ""))
        price_usd = float(sale_match.group(1).replace(",", ""))
        if face_value <= 0 or list_price_usd <= 0 or price_usd <= 0:
            continue

        face_key: float | int = int(face_value) if face_value.is_integer() else face_value
        products[face_value] = {
            "face_value_usd": face_key,
            "list_price_usd": round(list_price_usd, 4),
            "price_usd": round(price_usd, 4),
            "product_name": f"iTunes Gift Card {face_key} USD US",
        }

    result = [products[key] for key in sorted(products)]
    if len(result) < 5:
        raise RuntimeError(f"SEAGM 价格解析异常，仅识别到 {len(result)} 个面额")
    return result


def previous_fx_rate() -> float | None:
    latest = read_json(LATEST_FILE, {})
    try:
        value = float(latest["exchange_rate"]["usd_cny"])
        return value if value > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def fetch_usd_cny() -> tuple[float, str]:
    errors: list[str] = []

    try:
        response = requests.get(FRANKFURTER_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
        rate = float(response.json()["rates"]["CNY"])
        if rate > 0:
            return rate, "Frankfurter"
    except Exception as exc:  # noqa: BLE001 - 将失败切到备用数据源
        errors.append(f"Frankfurter: {exc}")

    try:
        response = requests.get(ER_API_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
        payload = response.json()
        rate = float(payload["rates"]["CNY"])
        if rate > 0:
            return rate, "open.er-api.com"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ER API: {exc}")

    old_rate = previous_fx_rate()
    if old_rate:
        return old_rate, "previous_successful_rate"

    raise RuntimeError("USD/CNY 汇率获取失败：" + " | ".join(errors))


def normalize_products(
    products: list[dict[str, float | int | str]], usd_cny: float
) -> list[dict[str, float | int | str]]:
    normalized = []
    for product in products:
        face_value = float(product["face_value_usd"])
        price_usd = float(product["price_usd"])
        price_cny = price_usd * usd_cny
        normalized.append(
            {
                **product,
                "price_cny": round(price_cny, 2),
                "cny_per_usd_value": round(price_cny / face_value, 4),
            }
        )
    return normalized


def append_history(snapshot: dict[str, Any]) -> None:
    history = read_json(HISTORY_FILE, {"snapshots": []})
    snapshots = history.get("snapshots")
    if history.get("region") != snapshot["source"]["region"] or not isinstance(snapshots, list):
        snapshots = []

    snapshots.append(
        {
            "checked_at": snapshot["checked_at"],
            "usd_cny": snapshot["exchange_rate"]["usd_cny"],
            "products": [
                {
                    "face_value_usd": item["face_value_usd"],
                    "list_price_usd": item["list_price_usd"],
                    "price_usd": item["price_usd"],
                    "price_cny": item["price_cny"],
                    "cny_per_usd_value": item["cny_per_usd_value"],
                }
                for item in snapshot["products"]
            ],
        }
    )

    # 页面只需要 30 天趋势，多保留一倍窗口便于后续统计，同时控制仓库体积。
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    retained = []
    for item in snapshots:
        try:
            dt = datetime.fromisoformat(str(item["checked_at"]).replace("Z", "+00:00"))
            if dt >= cutoff:
                retained.append(item)
        except (KeyError, TypeError, ValueError):
            continue

    write_json(
        HISTORY_FILE,
        {
            "region": snapshot["source"]["region"],
            "retention_days": 60,
            "snapshots": retained,
        },
    )


def main() -> None:
    html = fetch_html()
    raw_products = parse_products(html)
    usd_cny, fx_source = fetch_usd_cny()
    products = normalize_products(raw_products, usd_cny)

    checked_at = now_iso()
    latest = {
        "status": "ok",
        "checked_at": checked_at,
        "source": {
            "name": "SEAGM",
            "url": SEAGM_URL,
            "region": "US",
        },
        "exchange_rate": {
            "usd_cny": round(usd_cny, 6),
            "source": fx_source,
        },
        "payment": {
            "alipay": {
                "documented_support": True,
                "note": "支付宝可用性及实际手续费以 SEAGM 结算页为准",
            }
        },
        "products": products,
    }

    write_json(LATEST_FILE, latest)
    append_history(latest)

    best = min(products, key=lambda item: float(item["cny_per_usd_value"]))
    print(f"抓取成功：{len(products)} 个面额")
    print(f"USD/CNY: {usd_cny:.4f} ({fx_source})")
    print(
        "当前单位成本最低："
        f"{best['face_value_usd']} USD / ¥{best['cny_per_usd_value']} per USD"
    )


if __name__ == "__main__":
    main()
