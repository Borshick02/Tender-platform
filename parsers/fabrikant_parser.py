#!/usr/bin/env python3
"""
Парсер Фабрикант (https://www.fabrikant.ru/)
Поиск: GET /procedure/search?query=<запрос>; поле ввода — input[name="search"], placeholder «Введите запрос».
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
    output: Path = Path("fabrikant_results.json")
    headless: bool = True
    navigation_timeout: int = 60_000
    parse_delay: int = 3_000


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "FABRIKANT"
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


BASE_URL = "https://www.fabrikant.ru"
SEARCH_URL = "https://www.fabrikant.ru/procedure/search"


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = " ".join(value.split()).strip()
    if s and "&quot;" in s:
        s = s.replace("&quot;", '"')
    return s or None


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
    """URL поиска: query (и опционально page)."""
    params: dict = {"query": query}
    if page > 1:
        params["page"] = str(page)
    return SEARCH_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def _collect_from_page(page) -> List[TenderResult]:
    """Парсинг результатов: ссылки на процедуры, организатор, даты (Tailwind-разметка)."""
    results: List[TenderResult] = []
    seen_urls: set[str] = set()
    # Ссылки на процедуры (например /procedure/1005977)
    procedure_links = page.locator("a[href*='/procedure/']")
    for i in range(procedure_links.count()):
        try:
            link = procedure_links.nth(i)
            href = link.get_attribute("href") or ""
            if "/procedure/search" in href or "search" in href:
                continue
            url = _normalize_url(href)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            # ID из URL: /procedure/1005977 -> 1005977
            tender_id = None
            m = re.search(r"/procedure/(\d+)", href)
            if m:
                tender_id = m.group(1)
            title = _clean(link.inner_text(timeout=1_000)) or tender_id or "Процедура"
            # Родительский блок карточки для организатора и дат
            block = link.locator("xpath=ancestor::*[contains(@class, 'flex') or contains(@class, 'min-h')][position()<=5][1]").first
            if block.count() == 0:
                block = link.locator("xpath=ancestor::article | ancestor::div[.//*[contains(text(), 'Организатор') or contains(text(), 'Дата публикации')]][1]").first
            organizer = None
            publish_date = None
            deadline = None
            purchase_type = None
            if block.count() > 0:
                try:
                    block_text = block.inner_text(timeout=1_000)
                    if "Организатор" in block_text:
                        for line in block_text.split("\n"):
                            line = line.strip()
                            if line.startswith("Организатор") and len(line) > 12:
                                organizer = _clean(line.replace("Организатор", "").strip(" :"))
                                break
                            if organizer is None and len(line) > 20 and "Организатор" in block_text and block_text.index("Организатор") < block_text.index(line):
                                idx = block_text.find("Организатор")
                                rest = block_text[idx + 11:].strip()
                                first_line = rest.split("\n")[0].strip() if rest else ""
                                if first_line and len(first_line) > 5:
                                    organizer = _clean(first_line)
                                break
                    if "Дата публикации" in block_text:
                        for line in block_text.split("\n"):
                            if "Дата публикации" in line:
                                publish_date = _clean(re.sub(r"Дата публикации\s*:?\s*", "", line))
                                break
                    if "Дата окончания приёма заявок" in block_text or "окончания приёма" in block_text:
                        for line in block_text.split("\n"):
                            if "окончания приёма" in line or "окончания приема" in line:
                                deadline = _clean(re.sub(r".*окончания при[ёe]ма заявок\s*:?\s*", "", line, flags=re.I))
                                if not deadline:
                                    deadline = _clean(line)
                                break
                    if "Электронный аукцион" in block_text or "Запрос котировок" in block_text or "Конкурс" in block_text:
                        for t in ["Электронный аукцион", "Запрос котировок", "Открытый конкурс", "Конкурс"]:
                            if t in block_text:
                                purchase_type = t
                                break
                except Exception:
                    pass
            results.append(
                TenderResult(
                    title=title or "Процедура",
                    url=url,
                    organizer=organizer,
                    publish_date=publish_date,
                    deadline=deadline,
                    tender_id=tender_id,
                    purchase_type=purchase_type,
                )
            )
        except Exception:
            continue
    return results


def _go_next_page(page) -> bool:
    """Переход на следующую страницу."""
    next_btn = page.locator(
        "a[rel='next'], a:has-text('Следующая'), a:has-text('»'), a:has-text('›'), "
        "[class*='pagination'] a[href*='page='], a[href*='page=']"
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
    """Поиск на Фабрикант: GET procedure/search?query= или ввод в input[name='search']."""
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
            search_url = _build_search_url(cfg.query)
            print(f"Загрузка: {search_url[:80]}...")
            page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(2_000)

            # Если нет ссылок на процедуры — ввод в поле поиска
            if page.locator("a[href*='/procedure/']").count() == 0:
                search_input = page.locator(
                    "input[name='search'], input[placeholder='Введите запрос'], "
                    "input[placeholder*='запрос']"
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
    parser = argparse.ArgumentParser(description="Парсер Фабрикант (fabrikant.ru), поиск по query / поле «Введите запрос»")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("fabrikant_results.json"))
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
    print(f"Фабрикант. Запрос: {cfg.query}, страниц: {cfg.pages}")
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
