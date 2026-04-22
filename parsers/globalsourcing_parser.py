# -*- coding: utf-8 -*-
"""
Парсер товаров Global Sources (globalsources.com) — использует реальный Chrome
с stealth-скриптами и BeautifulSoup для парсинга карточек li.card-box.

Стратегия:
1. Сначала заходим на главную страницу для прохождения Incapsula challenge
2. Переходим на страницу поиска
3. Парсим карточки товаров через BeautifulSoup

Интерфейс: run_search(cfg) -> List[TenderResult]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Literal

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    from stealth_utils import apply_stealth
except ImportError:
    apply_stealth = None

if sys.platform == "win32":
    try:
        import codecs
        sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
        sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")
    except Exception:
        pass

_local_appdata = os.environ.get("LOCALAPPDATA")
if _local_appdata:
    _default_pw_path = str(Path(_local_appdata) / "ms-playwright")
    _pw_path = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").lower()
    if (not _pw_path) or ("cursor-sandbox-cache" in _pw_path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _default_pw_path


STEALTH_JS_INLINE = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            p.refresh = () => {};
            return p;
        }
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = {
        app: { isInstalled: false },
        runtime: { OnInstalledReason: {}, PlatformOs: { WIN: 'win' } }
    };
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params)
    );

    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return getParam.call(this, p);
    };
}
"""


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("globalsources_results.json")
    headless: bool = True
    navigation_timeout: int = 20_000
    parse_delay: int = 2_000
    mode: Literal["auto", "http", "playwright"] = "auto"
    http_timeout_s: float = 25.0
    use_chrome_profile: bool = False
    chrome_persistent_user_data_dir: Optional[Path] = None
    chrome_extension_dir: Optional[Path] = None
    chrome_launch_timeout: int = 45_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "GLOBAL SOURCES"
    supplier: Optional[str] = None
    price: Optional[str] = None
    image_url: Optional[str] = None
    moq: Optional[str] = None
    tags: Optional[List[str]] = None


BASE_URL = "https://www.globalsources.com"


def _lxml_ok() -> bool:
    try:
        import lxml  # noqa: F401
        BeautifulSoup("<html></html>", "lxml")
        return True
    except Exception:
        return False


def _bs4_parser() -> str:
    return "lxml" if _lxml_ok() else "html.parser"


def _parse_globalsources_page(html: str) -> List[TenderResult]:
    """Parse product cards from Global Sources search results using BeautifulSoup."""
    if not BS4_AVAILABLE:
        return _parse_globalsources_regex(html)

    soup = BeautifulSoup(html, _bs4_parser())
    results: List[TenderResult] = []

    cards = soup.select("li.item.card-box")
    if not cards:
        cards = soup.select("li.card-box")
    if not cards:
        cards = soup.find_all("li", class_=lambda c: c and "card-box" in c)

    for card in cards:
        try:
            product: dict = {}

            title_el = card.select_one("a.product-name")
            if title_el:
                product["title"] = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.globalsources.com" + href
                product["url"] = href
            else:
                continue

            price_el = card.select_one("span.price")
            if price_el:
                product["price"] = price_el.get_text(strip=True)

            img_el = card.select_one("img.img")
            if img_el:
                src = img_el.get("src") or img_el.get("data-src", "")
                if src and "default.png" not in src:
                    product["image_url"] = src
                alt = img_el.get("alt", "")
                if alt and not product.get("title"):
                    product["title"] = alt

            moq_el = card.select_one("i.desc")
            if moq_el:
                moq_parent = moq_el.find_parent("span")
                if moq_parent:
                    moq_text = moq_parent.get_text(" ", strip=True)
                    moq_text = " ".join(moq_text.split())
                    product["moq"] = moq_text

            name_div = card.select_one("div.name a")
            if name_div:
                product["supplier"] = name_div.get_text(strip=True)
                supplier_href = name_div.get("href", "")
                if supplier_href and supplier_href.startswith("//"):
                    supplier_href = "https:" + supplier_href

            tag_list = []
            for tag_img in card.select(".gs-tag-group img[alt]"):
                alt = tag_img.get("alt", "").strip()
                if alt:
                    tag_list.append(alt)

            results.append(
                TenderResult(
                    title=product.get("title", ""),
                    url=product.get("url", ""),
                    supplier=product.get("supplier"),
                    price=product.get("price"),
                    image_url=product.get("image_url"),
                    moq=product.get("moq"),
                    tags=tag_list if tag_list else None,
                )
            )

        except Exception:
            continue

    return results


def _parse_globalsources_regex(html: str) -> List[TenderResult]:
    """Fallback regex-based parser when BS4 is not available."""
    results: List[TenderResult] = []
    seen = set()

    for m in re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*>", html, flags=re.I):
        href = m.group(1)
        if "/product/" not in href:
            continue
        if href.startswith("//"):
            url = "https:" + href
        elif href.startswith("http"):
            url = href
        else:
            url = BASE_URL + href

        tag = m.group(0)
        mt = re.search(r"title=\"([^\"]{2,200})\"", tag, flags=re.I)
        title = mt.group(1).strip() if mt else None
        if not title:
            continue
        key = (url, title)
        if key in seen:
            continue
        seen.add(key)
        results.append(TenderResult(title=title, url=url))
        if len(results) >= 100:
            break

    return results


def _is_blocked_by_imperva_html(html: str) -> bool:
    h = html.lower()
    if "_incapsula_resource" in h:
        return True
    if "access denied" in h and "imperva" in h:
        return True
    if "error 15" in h and "imperva" in h:
        return True
    return False


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска на Global Sources."""
    print(f"[GlobalSources] Запуск Playwright (headless={cfg.headless})...")

    all_products: List[TenderResult] = []

    with sync_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
        ]
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=launch_args,
        )

        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
            print(f"[proxy] GlobalSources: {cfg.proxy.get('server', '?')}")

        context = browser.new_context(**ctx_kwargs)

        if apply_stealth:
            apply_stealth(context)
        else:
            context.add_init_script(STEALTH_JS_INLINE)

        page = context.new_page()

        try:
            for page_num in range(1, cfg.pages + 1):
                q = urllib.parse.quote_plus(cfg.query)
                url = (
                    f"https://www.globalsources.com/manufacturers/"
                    f"{q}.html?PageNumber={page_num}"
                )
                print(f"[GlobalSources] Page {page_num}/{cfg.pages}: {url}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                    page.wait_for_timeout(3000)

                    for wait_i in range(8):
                        try:
                            html_check = page.content()
                        except Exception:
                            page.wait_for_timeout(2000)
                            continue
                        if len(html_check) > 50000 and "Incapsula" not in html_check:
                            break
                        page.wait_for_timeout(2000)

                    html = page.content()
                    if _is_blocked_by_imperva_html(html):
                        print("[GlobalSources] Blocked by Imperva/Incapsula, stopping.")
                        break

                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, 800)")
                        page.wait_for_timeout(random.randint(800, 1500))

                    html = page.content()
                    products = _parse_globalsources_page(html)

                    if not products:
                        print(f"[GlobalSources] No products on page {page_num}, trying regex fallback...")
                        products = _parse_globalsources_regex(html)

                    if not products:
                        print(f"[GlobalSources] No products on page {page_num}, stopping.")
                        break

                    all_products.extend(products)
                    print(f"[GlobalSources] Found {len(products)} products on page {page_num}")

                    page.wait_for_timeout(random.randint(1500, 3000))

                except Exception as e:
                    print(f"[GlobalSources] Error on page {page_num}: {e}")
                    break

        except PlaywrightTimeoutError as e:
            print(f"[GlobalSources] Timeout: {e}", file=sys.stderr)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    print(f"[GlobalSources] Total: {len(all_products)} products")
    return all_products


def run_search_batch(queries: List[str], cfg: SearchConfig) -> dict[str, List[TenderResult]]:
    """Batch: search multiple queries with one browser instance."""
    print(f"[GlobalSources] Batch: {len(queries)} queries...")
    results: dict[str, List[TenderResult]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-infobars", "--disable-dev-shm-usage"],
        )
        ctx_kwargs = dict(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US", timezone_id="America/New_York",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
        context = browser.new_context(**ctx_kwargs)
        if apply_stealth:
            apply_stealth(context)
        else:
            context.add_init_script(STEALTH_JS_INLINE)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)

        for qi, q in enumerate(queries):
            print(f"  [{qi+1}/{len(queries)}] GS: '{q[:50]}'")
            q_enc = urllib.parse.quote_plus(q)
            url = f"https://www.globalsources.com/manufacturers/{q_enc}.html"
            items: List[TenderResult] = []
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                page.wait_for_timeout(3000)
                for _ in range(8):
                    try:
                        html_check = page.content()
                    except Exception:
                        page.wait_for_timeout(2000)
                        continue
                    if len(html_check) > 50000 and "Incapsula" not in html_check:
                        break
                    page.wait_for_timeout(2000)
                html = page.content()
                if not _is_blocked_by_imperva_html(html):
                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, 800)")
                        page.wait_for_timeout(random.randint(800, 1500))
                    html = page.content()
                    items = _parse_globalsources_page(html)
                    if not items:
                        items = _parse_globalsources_regex(html)
            except Exception as e:
                print(f"    Error: {e}")
            results[q] = items
            print(f"    -> {len(items)} items")

        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    total = sum(len(v) for v in results.values())
    print(f"[GlobalSources] Batch done: {total} total from {len(queries)} queries")
    return results


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер товаров Global Sources")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("globalsources_results.json"))
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
    try:
        results = run_search(cfg)
        save_results(cfg.output, results)
        print(f"Сохранено: {len(results)} в {cfg.output}")
        return 0
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
