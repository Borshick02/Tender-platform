#!/usr/bin/env python3
"""
Парсер ЗаказРФ (https://www.zakazrf.ru/NotificationEx)
Поиск: GET с параметром FastFilter; поле ввода — input#Filter_FastFilter (name="Filter.FastFilter").
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
    output: Path = Path("zakazrf_results.json")
    headless: bool = True
    navigation_timeout: int = 60_000
    parse_delay: int = 3_000


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "ZAKAZRF"
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


BASE_URL = "https://www.zakazrf.ru"
LIST_URL = "https://www.zakazrf.ru/NotificationEx"

# Параметры по умолчанию из запроса браузера (Payload)
DEFAULT_PARAMS = {
    "Filter": "1",
    "SelectedTabPage": "ALL",
    "IsConstructionProcurement": "0",
    "IsGroup": "0",
    "QuantityUndefined": "0",
    "ContractBlocked": "0",
    "AsPublic": "0",
}


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


def _build_search_url(query: str) -> str:
    """URL для поиска: FastFilter + остальные параметры."""
    params = {**DEFAULT_PARAMS, "FastFilter": query}
    return LIST_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def _collect_from_table(page) -> List[TenderResult]:
    """Парсинг таблицы результатов (table.reporttable, id TableList*)."""
    results: List[TenderResult] = []
    table = page.locator("table.reporttable")
    if table.count() == 0:
        table = page.locator("table[id^='TableList']")
    if table.count() == 0:
        return results

    rows = table.locator("tbody tr")
    n = rows.count()
    for i in range(n):
        row = rows.nth(i)
        try:
            cells = row.locator("td")
            nc = cells.count()
            if nc < 3:
                continue
            # Ссылка на процедуру — ищем в строке (часто в колонке «Номер закупки» или «Предмет закупки»)
            link = row.locator("a[href*='NotificationEx'], a[href*='/Notification/'], a[href*='View']").first
            if link.count() == 0:
                link = row.locator("a[href^='/'], a[href*='zakazrf']").first
            url = ""
            title = ""
            if link.count() > 0:
                url = link.get_attribute("href") or ""
                title = _clean(link.inner_text(timeout=1_000))
            url = _normalize_url(url)
            if not title and nc >= 3:
                title = _clean(cells.nth(2).inner_text(timeout=1_000))  # Предмет закупки
            if not title:
                title = _clean(cells.nth(0).inner_text(timeout=1_000)) or "Процедура"
            if not url and link.count() > 0:
                url = _normalize_url(link.get_attribute("href") or "")
            if not url:
                continue
            # Номер закупки (часто в первой колонке)
            tender_id = _clean(cells.nth(0).inner_text(timeout=500))
            if tender_id and re.match(r"^\d", tender_id):
                pass
            else:
                tender_id = None
            # Организатор — обычно 4-я колонка (индекс 3), Заказчик — 5-я (индекс 4)
            organizer = None
            customer = None
            if nc > 4:
                organizer = _clean(cells.nth(3).inner_text(timeout=500))
                customer = _clean(cells.nth(4).inner_text(timeout=500))
            elif nc > 3:
                organizer = _clean(cells.nth(3).inner_text(timeout=500))
            # Даты: публикация и окончание подачи заявок (колонки 6–8 по описанию)
            publish_date = None
            deadline = None
            if nc > 6:
                publish_date = _clean(cells.nth(6).inner_text(timeout=500))
            if nc > 8:
                deadline = _clean(cells.nth(8).inner_text(timeout=500))
            if not publish_date and nc > 5:
                publish_date = _clean(cells.nth(5).inner_text(timeout=500))
            # Способ закупки (вторая колонка) — purchase_type
            purchase_type = None
            if nc > 1:
                purchase_type = _clean(cells.nth(1).inner_text(timeout=500))

            results.append(
                TenderResult(
                    title=title or "Процедура",
                    url=url,
                    customer=customer,
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
    """Переход на следующую страницу (кнопка/ссылка «далее»)."""
    # ЗаказРФ: кнопки с onclick UpdateList_GotoPageNext*, или ссылка/кнопка со стрелкой
    next_btn = page.locator(
        "a[onclick*='GotoPageNext'], a[onclick*='GotoPageNext'], "
        "button[onclick*='GotoPageNext'], "
        "a:has-text('›'), a:has-text('»'), a:has-text('Следующая'), "
        ".filter-pager-inst a:has-text('>')"
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
    """Поиск на ЗаказРФ: GET с FastFilter или ввод в input#Filter_FastFilter."""
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
            # Поиск через GET с FastFilter (как в браузере)
            search_url = _build_search_url(cfg.query)
            print(f"Загрузка: {search_url[:80]}...")
            page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(2_000)

            # Альтернатива: если нужен ввод в форму — заполняем Filter_FastFilter и вызываем onchange
            table = page.locator("table.reporttable, table[id^='TableList']")
            if table.count() == 0:
                fast_input = page.locator("input#Filter_FastFilter, input[name='Filter.FastFilter']").first
                if fast_input.count() > 0:
                    print("    Ввод запроса в Filter_FastFilter...")
                    fast_input.fill(cfg.query)
                    page.wait_for_timeout(500)
                    fast_input.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
                    page.wait_for_timeout(1_500)
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(2_000)

            seen: set[str] = set()
            for page_num in range(cfg.pages):
                print(f"Страница {page_num + 1}/{cfg.pages}...")
                page_results = _collect_from_table(page)
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
    parser = argparse.ArgumentParser(description="Парсер ЗаказРФ NotificationEx")
    parser.add_argument("query", help="Поисковый запрос (ключевые слова)")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("zakazrf_results.json"))
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
    print(f"ЗаказРФ. Запрос: {cfg.query}, страниц: {cfg.pages}")
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
