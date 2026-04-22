#!/usr/bin/env python3
"""
Парсер Росэлторг (https://www.roseltorg.ru/)
Поиск: GET /procedures/search?sale=1&query_field=<запрос>; поле ввода — input[name="query_field"], .search-box__input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("roseltorg_results.json")
    headless: bool = True
    navigation_timeout: int = 60_000
    parse_delay: int = 3_000


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "ROSELTORG"
    customer: Optional[str] = None
    organizer: Optional[str] = None
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None
    deadline: Optional[str] = None
    region: Optional[str] = None
    tender_id: Optional[str] = None
    law_type: Optional[str] = None
    purchase_type: Optional[str] = None


BASE_URL = "https://www.roseltorg.ru"
SEARCH_URL = "https://www.roseltorg.ru/procedures/search"


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(value.split()).strip() or None


def _normalize_url(href: str) -> str:
    if not href or not href.strip():
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    return BASE_URL + "/" + href


def _build_search_url(query: str, page: int = 1) -> str:
    """URL поиска: query_field + sale=1, опционально page."""
    params: dict = {"sale": "1", "query_field": query}
    if page > 1:
        params["page"] = str(page)
    return SEARCH_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def _collect_from_page(page) -> List[TenderResult]:
    """Парсинг результатов: карточки с .search-results_lot, .search-results_customer, .search-results_region."""
    results: List[TenderResult] = []
    seen_urls: set[str] = set()
    # Блоки-карточки: содержат ссылку на процедуру и блок заказчика/региона
    cards = page.locator(
        "div:has(a[href^='/procedure/']):has(.search-results_customer), "
        "div:has(.search-results_lot):has(.search-results_customer)"
    )
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            link = card.locator("a[href^='/procedure/']").first
            if link.count() == 0:
                continue
            href = link.get_attribute("href") or ""
            url = _normalize_url(href)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            tender_id = href.split("/procedure/")[-1].split("?")[0].strip("/") or None
            title = _clean(link.inner_text(timeout=1_000)) or tender_id or "Процедура"
            organizer = None
            if card.locator(".search-results_customer p").count() > 0:
                organizer = _clean(card.locator(".search-results_customer p").first.inner_text(timeout=500))
            region = None
            if card.locator(".search-results_region p, [title='Регион заказчика']").count() > 0:
                region = _clean(card.locator(".search-results_region p, [title='Регион заказчика']").first.inner_text(timeout=500))
            if not title or re.match(r"^\(?Лот\s+\d+\)?$", (title or "").strip()):
                tit_el = card.locator(".search-results_title:not(.search-results_title--small), h3").first
                if tit_el.count() > 0:
                    t = _clean(tit_el.inner_text(timeout=500))
                    if t:
                        title = t
            results.append(
                TenderResult(
                    title=title or "Процедура",
                    url=url,
                    organizer=organizer,
                    region=region,
                    tender_id=tender_id,
                )
            )
        except Exception:
            continue
    # Если карточек не нашли — собираем по всем ссылкам на процедуры
    if not results:
        procedure_links = page.locator("a[href^='/procedure/']")
        for i in range(procedure_links.count()):
            try:
                link = procedure_links.nth(i)
                href = link.get_attribute("href") or ""
                url = _normalize_url(href)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                tender_id = href.split("/procedure/")[-1].split("?")[0].strip("/") or None
                title = _clean(link.inner_text(timeout=1_000)) or tender_id or "Процедура"
                results.append(
                    TenderResult(title=title or "Процедура", url=url, tender_id=tender_id)
                )
            except Exception:
                continue
    return results


def _go_next_page(page) -> bool:
    """Переход на следующую страницу."""
    next_btn = page.locator(
        "a[rel='next'], a:has-text('Следующая'), a:has-text('»'), a:has-text('›'), "
        ".pagination a.next, [class*='pagination'] a[href*='page=']"
    ).first
    if next_btn.count() > 0:
        try:
            if next_btn.is_visible(timeout=2_000):
                next_btn.click()
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(2_000)
                return True
        except Exception:
            pass
    return False


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Поиск на Росэлторг: GET с query_field или ввод в input[name='query_field']."""
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")
    collected: List[TenderResult] = []

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
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        try:
            # Поиск через GET (как в браузере)
            search_url = _build_search_url(cfg.query)
            print(f"Загрузка: {search_url[:85]}...")
            page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(2_000)

            # Если результатов нет — пробуем ввод в поле поиска
            if page.locator("a[href^='/procedure/']").count() == 0:
                search_input = page.locator(
                    "input[name='query_field'], input.search-box__input, "
                    "input[placeholder*='ключевое слово']"
                ).first
                if search_input.count() > 0:
                    print("    Ввод запроса в поле поиска...")
                    search_input.fill(cfg.query)
                    page.wait_for_timeout(500)
                    search_input.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(2_000)

            seen: set[str] = set()
            for page_num in range(cfg.pages):
                print(f"Страница {page_num + 1}/{cfg.pages}...")
                if page_num >= 1:
                    page_url = _build_search_url(cfg.query, page=page_num + 1)
                    page.goto(page_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                    page.wait_for_timeout(2_000)
                page_results = _collect_from_page(page)
                new_rows = [r for r in page_results if r.url not in seen]
                seen.update(r.url for r in new_rows)
                collected.extend(new_rows)
                print(f"  Собрано: {len(new_rows)} (всего: {len(collected)})")
                if page_num + 1 >= cfg.pages:
                    break
                if not _go_next_page(page):
                    break
                page.wait_for_timeout(cfg.parse_delay)

        except PlaywrightTimeoutError as e:
            print(f"Таймаут: {e}")
        except Exception as e:
            print(f"Ошибка: {e}")
            import traceback
            traceback.print_exc()
        finally:
            context.close()
            browser.close()

    return collected


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер Росэлторг (roseltorg.ru), поиск по query_field")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("roseltorg_results.json"))
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
    print(f"Росэлторг. Запрос: {cfg.query}, страниц: {cfg.pages}")
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
