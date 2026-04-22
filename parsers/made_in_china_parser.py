# -*- coding: utf-8 -*-
"""
Парсер товаров с Made-in-China.com (ru.made-in-china.com) по поисковому запросу.

Логика:
- открываем страницу поиска /productSearch?keyword=...
- забираем карточки из .search-list .list-node (или .prod-list .list-node)
- для каждой карточки: title, url, price (FOB $), shop_name, image_url, moq
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

try:
    from stealth_utils import apply_stealth
except ImportError:
    apply_stealth = None


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("made_in_china_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000
    parse_delay: int = 3_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "MADE-IN-CHINA.COM"
    price: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    moq: Optional[str] = None
    shop_name: Optional[str] = None
    image_url: Optional[str] = None
    location: Optional[str] = None


BASE_URL = "https://ru.made-in-china.com"
SEARCH_PATH = "/productSearch"


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    stripped = " ".join(text.split())
    return stripped or None


def _safe_inner_text(loc: Locator, timeout: int = 2_000) -> Optional[str]:
    try:
        if loc.count() == 0:
            return None
        return loc.first.inner_text(timeout=timeout)
    except Exception:
        return None


def _safe_attribute(loc: Locator, name: str, timeout: int = 2_000) -> Optional[str]:
    try:
        if loc.count() == 0:
            return None
        return loc.first.get_attribute(name)
    except Exception:
        return None


def _parse_price(spans_text: str) -> tuple[Optional[float], Optional[float]]:
    """Парсит строку вида '0,39-0,71' или '0,30' в (min, max)."""
    if not spans_text:
        return None, None
    text = spans_text.replace(",", ".").strip()
    if "-" in text:
        parts = text.split("-", 1)
        try:
            lo = float(parts[0].strip())
            hi = float(parts[1].strip())
            return lo, hi
        except (ValueError, IndexError):
            return None, None
    try:
        v = float(text)
        return v, v
    except ValueError:
        return None, None


def collect_offers(page: Page, cfg: SearchConfig) -> List[TenderResult]:
    """Сбор карточек товаров с ru.made-in-china.com."""
    results: List[TenderResult] = []

    # Карточки: .search-list .list-node или внутри .prod-list
    nodes = page.locator(".search-list .list-node, .prod-list .list-node")
    count = nodes.count()
    if count == 0:
        return results

    max_items = min(count, 40)

    for i in range(max_items):
        card = nodes.nth(i)
        try:
            # Ссылка на товар
            link = card.locator("h2.product-name a, a.product-detail").first
            url = _safe_attribute(link, "href") or ""
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = BASE_URL.rstrip("/") + url

            # Заголовок
            title_loc = card.locator("h2.product-name a").first
            title = _clean(_safe_inner_text(title_loc))
            if not title:
                title_loc = card.locator("a.product-detail").first
                title = _clean(_safe_inner_text(title_loc)) or ""

            # Цена: .product-property .price-info strong.price
            price_block = card.locator(".product-property .price-info strong.price")
            price_raw = _clean(_safe_inner_text(price_block))
            price_min, price_max = None, None
            if price_raw:
                price_min, price_max = _parse_price(price_raw)

            # MOQ: второй .info в .product-property
            info_blocks = card.locator(".product-property .info")
            moq = None
            if info_blocks.count() >= 2:
                moq = _clean(_safe_inner_text(info_blocks.nth(1)))

            # Компания
            company_loc = card.locator("a.compnay-name, .compnay-name").first
            shop_name = _clean(_safe_inner_text(company_loc))

            # Изображение
            img_loc = card.locator(".prod-img img, .img-thumb-inner img").first
            image_url = _safe_attribute(img_loc, "data-original") or _safe_attribute(img_loc, "src")
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

            results.append(
                TenderResult(
                    title=title or "—",
                    url=url,
                    price=price_raw,
                    price_min=price_min,
                    price_max=price_max,
                    moq=moq,
                    shop_name=shop_name,
                    image_url=image_url,
                )
            )
        except Exception:
            continue

    return results


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска на Made-in-China.com."""
    print(f"Запуск Playwright для Made-in-China.com (headless={cfg.headless})...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx_kwargs = dict(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
            print(f"[proxy] Made-in-China: используется прокси {cfg.proxy.get('server', '?')}")
        context = browser.new_context(**ctx_kwargs)
        if apply_stealth:
            apply_stealth(context)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        collected: List[TenderResult] = []

        try:
            query_encoded = urllib.parse.quote(cfg.query)
            print(f"Загрузка Made-in-China.com для запроса: '{cfg.query}'")

            for page_num in range(1, max(1, int(cfg.pages)) + 1):
                search_url = (
                    f"{BASE_URL}{SEARCH_PATH}"
                    f"?keyword={query_encoded}&searchLanguageCode=5&page={page_num}"
                )
                print(f"URL: {search_url}")

                page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)

                # Разметка/антибот у Made-in-China меняются. Не зависаем на одном селекторе.
                wait_selectors = [
                    ".search-list .list-node",
                    ".prod-list .list-node",
                    ".list-node",
                    "a.product-detail",
                    "h2.product-name a",
                ]
                found = False
                for sel in wait_selectors:
                    try:
                        page.wait_for_selector(sel, timeout=7_000)
                        if page.locator(sel).count() > 0:
                            found = True
                            break
                    except PlaywrightTimeoutError:
                        continue

                if not found:
                    try:
                        if page.locator("text=/captcha|verify|access denied|incapsula|imperva/i").count() > 0:
                            print("⛔ Made-in-China.com: похоже на антибот/капчу.", file=sys.stderr)
                    except Exception:
                        pass
                    page.wait_for_timeout(1_000)

                page.wait_for_timeout(cfg.parse_delay)
                batch = collect_offers(page, cfg)
                collected.extend(batch)

            print(f"✅ Made-in-China.com: собрано {len(collected)} предложений")

        except PlaywrightTimeoutError as e:
            print(f"Таймаут Made-in-China.com: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()

        return collected


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(
        description="Парсер товаров Made-in-China.com по ключевым словам"
    )
    parser.add_argument("query", help="Поисковый запрос (напр.: перчатки)")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("made_in_china_results.json"))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--timeout", type=int, default=60_000)
    args = parser.parse_args(argv)
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
    )


def main(argv: List[str]) -> int:
    cfg = parse_args(argv)
    print(f"Made-in-China.com. Запрос: {cfg.query}, страниц: {cfg.pages}")
    try:
        results = run_search(cfg)
        save_results(cfg.output, results)
        print(f"Сохранено: {len(results)} предложений в {cfg.output}")
        return 0
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
