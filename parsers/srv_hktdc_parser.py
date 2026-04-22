# -*- coding: utf-8 -*-
"""
Парсер товаров с HKTDC Sourcing (sourcing.hktdc.com) по поисковому запросу.

URL: /en/Product-Search/{query}/1
Карточки: .product-card
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("hktdc_results.json")
    headless: bool = True
    navigation_timeout: int = 60_000
    parse_delay: int = 3_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "HKTDC"
    price: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    moq: Optional[str] = None
    shop_name: Optional[str] = None
    image_url: Optional[str] = None
    location: Optional[str] = None


BASE_URL = "https://sourcing.hktdc.com"

_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


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


def _parse_price(text: str) -> tuple:
    if not text:
        return None, None, None
    raw = text.strip()
    numbers = re.findall(r'[\d]+(?:[\.,]\d+)?', raw.replace(",", ""))
    if not numbers:
        return raw, None, None
    try:
        vals = [float(n.replace(",", ".")) for n in numbers]
        if len(vals) == 1:
            return raw, vals[0], vals[0]
        return raw, vals[0], vals[1]
    except (ValueError, IndexError):
        return raw, None, None


def collect_offers(page: Page, cfg: SearchConfig) -> List[TenderResult]:
    results: List[TenderResult] = []

    cards = page.locator(".product-card")
    count = cards.count()
    if count == 0:
        return results

    max_items = min(count, 40)

    for i in range(max_items):
        card = cards.nth(i)
        try:
            links = card.locator("a")
            url = ""
            title = ""
            if links.count() > 0:
                for li in range(min(links.count(), 5)):
                    href = links.nth(li).get_attribute("href") or ""
                    if "/Product-Detail/" in href or "/product/" in href.lower():
                        url = href
                        txt = _clean(links.nth(li).inner_text(timeout=2000))
                        if txt and len(txt) > 3:
                            title = txt
                        break

            if not url:
                href = links.first.get_attribute("href") if links.count() > 0 else ""
                url = href or ""

            if not url:
                continue

            if url.startswith("/"):
                url = BASE_URL + url

            if not title:
                title_parts = []
                for li in range(min(links.count(), 3)):
                    txt = _clean(links.nth(li).inner_text(timeout=1500))
                    if txt and len(txt) > 3 and "enquire" not in txt.lower():
                        title_parts.append(txt)
                        break
                title = title_parts[0] if title_parts else ""

            if not title:
                continue

            card_text = card.inner_text(timeout=3000)
            price_text = ""
            price_match = re.search(r'USD[\s]?[\d.,]+(?:\s*[-–]\s*[\d.,]+)?(?:\s*/\s*[\w()]+)?', card_text)
            if price_match:
                price_text = price_match.group(0).strip()
                title = re.sub(r'\s*USD[\s]?[\d.,].*$', '', title).strip()

            raw_price, price_min, price_max = _parse_price(price_text)

            supplier_loc = card.locator("a[href*='Supplier-Store'], a[href*='supplier']")
            shop_name = _clean(_safe_inner_text(supplier_loc))

            img_loc = card.locator("img")
            image_url = None
            if img_loc.count() > 0:
                src = _safe_attribute(img_loc, "src") or _safe_attribute(img_loc, "data-src") or ""
                if src and not src.endswith(".svg") and "icon" not in src.lower():
                    if src.startswith("//"):
                        src = "https:" + src
                    image_url = src

            results.append(
                TenderResult(
                    title=title,
                    url=url,
                    price=raw_price,
                    price_min=price_min,
                    price_max=price_max,
                    shop_name=shop_name,
                    image_url=image_url,
                )
            )
        except Exception:
            continue

    return results


def _make_browser(p, cfg):
    browser = p.chromium.launch(
        headless=cfg.headless,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    ctx_kw = dict(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
    )
    if cfg.proxy:
        ctx_kw["proxy"] = cfg.proxy
    context = browser.new_context(**ctx_kw)
    context.add_init_script(_INIT_SCRIPT)
    return browser, context


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    print(f"[HKTDC] query='{cfg.query}' headless={cfg.headless}")
    with sync_playwright() as p:
        browser, context = _make_browser(p, cfg)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)
        collected: List[TenderResult] = []
        try:
            query_encoded = urllib.parse.quote(cfg.query)
            search_url = f"{BASE_URL}/en/Product-Search/{query_encoded}/1"
            page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            try:
                page.wait_for_selector(".product-card", timeout=15_000)
            except Exception:
                page.wait_for_timeout(3_000)
            page.wait_for_timeout(cfg.parse_delay)
            collected = collect_offers(page, cfg)
            print(f"[HKTDC] '{cfg.query}' -> {len(collected)} items")
        except PlaywrightTimeoutError as e:
            print(f"[HKTDC] timeout: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()
        return collected


def run_search_batch(queries: List[str], cfg: SearchConfig) -> Dict[str, list]:
    """Batch: one browser, multiple queries."""
    result_map: Dict[str, list] = {}
    print(f"[HKTDC batch] {len(queries)} queries, headless={cfg.headless}")
    with sync_playwright() as p:
        browser, context = _make_browser(p, cfg)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        for qi, q in enumerate(queries):
            print(f"[HKTDC batch] [{qi+1}/{len(queries)}] '{q}'")
            try:
                encoded = urllib.parse.quote(q)
                url = f"{BASE_URL}/en/Product-Search/{encoded}/1"
                page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                try:
                    page.wait_for_selector(".product-card", timeout=10_000)
                except Exception:
                    page.wait_for_timeout(2_000)
                page.wait_for_timeout(cfg.parse_delay)
                items = collect_offers(page, cfg)
                for it in items:
                    it.source = "HKTDC"
                result_map[q] = [asdict(r) for r in items]
                print(f"[HKTDC batch]   -> {len(items)} items")
            except Exception as e:
                print(f"[HKTDC batch]   error: {e}")
                traceback.print_exc()
                result_map[q] = []
                try:
                    page.close()
                    page = context.new_page()
                    page.set_default_navigation_timeout(cfg.navigation_timeout)
                    page.set_default_timeout(cfg.navigation_timeout)
                except Exception:
                    pass
            time.sleep(1)

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    return result_map


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HKTDC Sourcing Parser")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--output", type=str, default="hktdc_results.json")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()
    cfg = SearchConfig(query=args.query, pages=args.pages, output=Path(args.output), headless=args.headless)
    results = run_search(cfg)
    output_data = [asdict(r) for r in results]
    Path(args.output).write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results saved to {args.output}")
