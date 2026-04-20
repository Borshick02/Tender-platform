from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Locator

# См. аналогичную правку в multi_parser.py — иногда PLAYWRIGHT_BROWSERS_PATH указывает на временную папку.
_local_appdata = os.environ.get("LOCALAPPDATA")
if _local_appdata:
    _default_pw_path = str(Path(_local_appdata) / "ms-playwright")
    _pw_path = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").lower()
    if (not _pw_path) or ("cursor-sandbox-cache" in _pw_path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _default_pw_path


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("synapse_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 5_000  # ms
    include_positions: bool = False  # извлекать «Позиции» со страницы тендера
    positions_limit: int = 50  # максимум строк из таблицы «Позиции» на тендер
    positions_timeout: int = 20_000  # ms
    positions_max_tenders: int = 20  # ограничение числа тендеров для детализации


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "SYNAPSE"  # Источник данных
    customer: Optional[str] = None
    organizer: Optional[str] = None
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None
    deadline: Optional[str] = None
    region: Optional[str] = None
    law_type: Optional[str] = None  # 44-ФЗ, 223-ФЗ
    purchase_type: Optional[str] = None
    platform: Optional[str] = None  # Площадка размещения
    inn: Optional[str] = None
    okpd2: Optional[str] = None  # ОКПД2 код
    positions: Optional[List[dict]] = None  # позиции поставки (из карточки тендера)


def _extract_positions_from_tender_page(page: Page, *, limit: int) -> List[dict]:
    """
    Пытается извлечь таблицу «Позиции» (как на synapsenet.ru в детальной карточке тендера).
    Возвращает список словарей вида: {name, quantity, document}.
    """
    # На synapsenet встречается заголовок «Позиции» и таблица с колонками.
    # Используем максимально мягкие селекторы.
    table = page.locator("table").filter(has_text="Наименование позиции").first
    if table.count() == 0:
        return []

    rows = table.locator("tbody tr")
    if rows.count() == 0:
        # иногда таблица может быть без tbody
        rows = table.locator("tr").filter(has=table.locator("td"))

    out: List[dict] = []
    max_rows = min(rows.count(), max(0, int(limit)))
    for i in range(max_rows):
        tr = rows.nth(i)
        tds = tr.locator("td")
        if tds.count() == 0:
            continue
        name = _clean(tds.nth(0).inner_text(timeout=2_000)) if tds.count() >= 1 else None
        qty = _clean(tds.nth(1).inner_text(timeout=2_000)) if tds.count() >= 2 else None
        doc = _clean(tds.nth(2).inner_text(timeout=2_000)) if tds.count() >= 3 else None
        if not name:
            continue
        out.append(
            {
                "name": name,
                "quantity": qty,
                "document": doc,
            }
        )
    return out


def fetch_positions(context, tender_url: str, cfg: SearchConfig) -> List[dict]:
    """Открывает карточку тендера Synapse и забирает раздел «Позиции»."""
    detail = context.new_page()
    detail.set_default_navigation_timeout(cfg.navigation_timeout)
    detail.set_default_timeout(cfg.navigation_timeout)
    try:
        detail.goto(tender_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
        # ждём появления слова «Позиции» (или таблицы)
        try:
            detail.wait_for_selector("text=Позиции", timeout=cfg.positions_timeout)
        except PlaywrightTimeoutError:
            # всё равно попробуем прочитать таблицу
            pass
        return _extract_positions_from_tender_page(detail, limit=cfg.positions_limit)
    finally:
        detail.close()


# ---------------------------
# Вспомогательные функции
# ---------------------------

def _safe_inner_text(scope: Locator, selector: str) -> Optional[str]:
    """Безопасное извлечение текста из элемента."""
    try:
        loc = scope.locator(selector)
        if loc.count() == 0:
            return None
        return loc.first.inner_text(timeout=2_000)
    except Exception:
        return None


def _clean(value: Optional[str]) -> Optional[str]:
    """Очистка строки от лишних пробелов."""
    if value is None:
        return None
    stripped = " ".join(value.split())
    return stripped or None


def _normalize_url(href: str, base_url: str) -> str:
    """Приведение относительных URL к абсолютным."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return "https://synapsenet.ru" + href
    if base_url.endswith("/"):
        return base_url + href
    return base_url.rsplit("/", 1)[0] + "/" + href


def countdown_timer(seconds: int, message: str = "Ожидание") -> None:
    """Обратный отсчет с выводом в консоль."""
    for remaining in range(seconds, 0, -1):
        print(f"\r{message}: {remaining} сек... ", end="", flush=True)
        time.sleep(1)
    print(f"\r{message}: завершено!     ")


def find_search_input(page: Page) -> Optional[Locator]:
    """Поиск поля ввода для поискового запроса на synapsenet.ru."""
    search_selectors = [
        # Специфичные для synapsenet.ru (точная структура)
        "input.sib-input",  # Основной селектор Synapse
        "#search-input-script",
        "input[placeholder*='Название товара']",
        "input[placeholder*='Найти тендеры']",
        "input[placeholder*='Поиск']",
        "input[name='search']",
        "input[name='q']",
        "input[type='search']",
        "input[type='text']",
        "#search",
        ".search-input",
        "input[placeholder*='поиск']",
        ".search__input",
        "form input[type='text']",
    ]
    
    for selector in search_selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                first = loc.first
                if first.is_visible(timeout=1_000):
                    print(f"    ✓ Найдено поле поиска: {selector}")
                    return first
        except Exception:
            continue
    
    return None


# ---------------------------
# Сбор результатов со страницы Synapse
# ---------------------------

def parse_synapse_tenders(tender_items: Locator) -> List[TenderResult]:
    """
    Парсинг карточек тендеров Synapse
    
    Synapse может использовать различные структуры для карточек тендеров
    """
    results: List[TenderResult] = []
    
    for i in range(tender_items.count()):
        tender = tender_items.nth(i)
        try:
            # Название тендера
            title = None
            title_selectors = [
                "a.tender-title",
                ".tender__title",
                "h3 a",
                "h4 a",
                ".title a",
                "a[href*='tender']",
                "a[href*='purchase']",
            ]
            
            link_elem = None
            for sel in title_selectors:
                elem = tender.locator(sel).first
                if elem.count() > 0:
                    link_elem = elem
                    title = elem.inner_text(timeout=2_000).strip()
                    if title:
                        break
            
            if not title:
                # Fallback: любая ссылка
                link_elem = tender.locator("a[href]").first
                if link_elem.count() > 0:
                    title = link_elem.inner_text(timeout=2_000).strip()
            
            if not title:
                continue
            
            # URL
            url = ""
            if link_elem and link_elem.count() > 0:
                url = link_elem.get_attribute("href") or ""
            
            # Получаем весь текст карточки для извлечения данных
            full_text = tender.inner_text()
            
            # Тип закона (44-ФЗ, 223-ФЗ)
            law_type = None
            if '44-ФЗ' in full_text or '44ФЗ' in full_text:
                law_type = '44-ФЗ'
            elif '223-ФЗ' in full_text or '223ФЗ' in full_text:
                law_type = '223-ФЗ'
            elif '615-ФЗ' in full_text:
                law_type = '615-ФЗ'
            
            # Цена
            price = None
            price_patterns = [
                r'(\d+[\s\d]*)\s*(?:руб|₽)',
                r'[Цц]ена[:\s]+(\d+[\s\d]*)',
                r'[Сс]умма[:\s]+(\d+[\s\d]*)',
                r'НМЦК[:\s]+(\d+[\s\d]*)',
            ]
            
            for pattern in price_patterns:
                match = re.search(pattern, full_text)
                if match:
                    price_value = match.group(1).replace(' ', '')
                    if len(price_value) > 3:  # Минимальная цена
                        price = f"{match.group(1).strip()} ₽"
                        break
            
            # Заказчик
            customer = None
            customer_patterns = [
                r'[Зз]аказчик[:\s]+([^\n]+)',
                r'[Оо]рганизация[:\s]+([^\n]+)',
            ]
            
            for pattern in customer_patterns:
                match = re.search(pattern, full_text)
                if match:
                    customer = _clean(match.group(1)[:100])  # Первые 100 символов
                    break
            
            # ИНН
            inn = None
            inn_match = re.search(r'ИНН[:\s]+(\d{10,12})', full_text)
            if inn_match:
                inn = inn_match.group(1)
            
            # Регион
            region = None
            region_patterns = [
                r'[Рр]егион[:\s]+([^\n]+)',
                r'([А-Яа-я\s-]+(?:область|край|республика|АО))',
            ]
            
            for pattern in region_patterns:
                match = re.search(pattern, full_text)
                if match:
                    region = _clean(match.group(1)[:50])
                    break
            
            # Срок подачи заявок
            deadline = None
            deadline_patterns = [
                r'[Дд]о[:\s]+(\d{2}\.\d{2}\.\d{4})',
                r'[Оо]кончание[:\s]+(\d{2}\.\d{2}\.\d{4})',
                r'[Сс]рок[:\s]+(\d{2}\.\d{2}\.\d{4})',
            ]
            
            for pattern in deadline_patterns:
                match = re.search(pattern, full_text)
                if match:
                    deadline = match.group(1)
                    break
            
            # Дата публикации
            publish_date = None
            publish_patterns = [
                r'[Оо]публиковано[:\s]+(\d{2}\.\d{2}\.\d{4})',
                r'[Дд]ата[:\s]+(\d{2}\.\d{2}\.\d{4})',
            ]
            
            for pattern in publish_patterns:
                match = re.search(pattern, full_text)
                if match:
                    publish_date = match.group(1)
                    break
            
            # Тип закупки
            purchase_type = None
            if 'аукцион' in full_text.lower():
                purchase_type = 'Электронный аукцион'
            elif 'конкурс' in full_text.lower():
                purchase_type = 'Конкурс'
            elif 'котировок' in full_text.lower():
                purchase_type = 'Запрос котировок'
            elif 'предложений' in full_text.lower():
                purchase_type = 'Запрос предложений'
            
            # Площадка
            platform = None
            platform_patterns = [
                r'[Пп]лощадка[:\s]+([^\n]+)',
                r'(ЭТП[:\s]+[^\n]+)',
            ]
            
            for pattern in platform_patterns:
                match = re.search(pattern, full_text)
                if match:
                    platform = _clean(match.group(1)[:50])
                    break
            
            # Фильтруем старые тендеры (2021-2023)
            if deadline and any(year in str(deadline) for year in ['2021', '2022', '2023']):
                continue
            if publish_date and any(year in str(publish_date) for year in ['2021', '2022', '2023']):
                continue
            
            results.append(
                TenderResult(
                    title=_clean(title) or title.strip(),
                    url=_normalize_url(url, "https://synapsenet.ru"),
                    source="SYNAPSE",
                    customer=customer,
                    price=price,
                    law_type=law_type,
                    purchase_type=purchase_type,
                    deadline=deadline,
                    publish_date=publish_date,
                    region=region,
                    platform=platform,
                    inn=inn,
                )
            )
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге тендера {i+1}: {e}")
            continue
    
    return results


def parse_synapse_specific_cards(tender_items: Locator) -> List[TenderResult]:
    """
    Парсинг карточек Synapse со специфичной структурой div.sp-tender-block
    """
    results: List[TenderResult] = []
    
    for i in range(tender_items.count()):
        card = tender_items.nth(i)
        try:
            # Заголовок (обязательное)
            title_elem = card.locator("a.sp-tb-title").first
            if title_elem.count() == 0:
                continue
            
            title_text = title_elem.inner_text(timeout=2_000)
            # Убираем HTML тэги из заголовка
            title = re.sub(r'<[^>]+>', '', title_text).strip()
            if not title:
                continue
            
            # URL
            url = title_elem.get_attribute("href") or ""
            
            # Номер закупки
            purchase_number = None
            number_elem = card.locator(".sp-tb-right-block .pro-open-form").first
            if number_elem.count() > 0:
                purchase_number = _clean(number_elem.inner_text(timeout=1_000))
            
            # Цена
            price = None
            price_block = card.locator(".sp-tb-right-block > div:has-text('начальная цена')").first
            if price_block.count() > 0:
                price_text = price_block.inner_text()
                # Извлекаем число и валюту
                price_match = re.search(r'([\d\s]+)\s*₽', price_text.replace('&nbsp;', ' '))
                if price_match:
                    price = f"{price_match.group(1).strip()} ₽"
            
            # Заказчик
            customer = None
            customer_elem = card.locator(".pro-pcr-grey-before:has-text('заказчик')").first
            if customer_elem.count() > 0:
                customer_link = customer_elem.locator("a.pro-link").first
                if customer_link.count() > 0:
                    customer = _clean(customer_link.inner_text(timeout=1_000))
            
            # Статус
            status = None
            status_elem = card.locator(".pro-pc-status").first
            if status_elem.count() > 0:
                status = _clean(status_elem.inner_text(timeout=1_000))
            
            # Площадка и тип закона
            platform = None
            law_type = None
            source_elem = card.locator(".sp-tb-source").first
            if source_elem.count() > 0:
                source_text = source_elem.inner_text()
                
                # Площадка
                platform_match = re.search(r'площадка\s+([^\n·•]+)', source_text)
                if platform_match:
                    platform = _clean(platform_match.group(1))
                
                # Тип закона
                if '44-ФЗ' in source_text:
                    law_type = '44-ФЗ'
                elif '223-ФЗ' in source_text:
                    law_type = '223-ФЗ'
                elif '615-ФЗ' in source_text:
                    law_type = '615-ФЗ'
            
            # Тип закупки
            purchase_type = None
            if source_elem and source_elem.count() > 0:
                source_text = source_elem.inner_text()
                type_match = re.search(r'способ отбора\s+([^\n·•]+)', source_text)
                if type_match:
                    purchase_type = _clean(type_match.group(1))
            
            # Даты приема заявок
            deadline = None
            publish_date = None
            time_elem = card.locator(".pro-pcr-desc:has(.pro-img-time)").first
            if time_elem.count() > 0:
                time_text = time_elem.inner_text()
                # Формат: 09:32 · 13.12.2025 — 11:32 · 13.12.2025
                dates = re.findall(r'(\d{2}\.\d{2}\.\d{4})', time_text)
                if len(dates) >= 1:
                    publish_date = dates[0]
                if len(dates) >= 2:
                    deadline = dates[1]
            
            # Дополнительная информация о времени до дедлайна
            attention_elem = card.locator(".pro-pcr-desc:has(.pro-img-atten)").first
            if attention_elem.count() > 0 and not deadline:
                atten_text = attention_elem.inner_text()
                if 'менее' in atten_text.lower() or 'осталось' in atten_text.lower():
                    # Используем как дополнительную информацию к статусу
                    if status:
                        status = f"{status} ({_clean(atten_text.split('для подачи заявки')[-1]).strip()})"
            
            # Регион
            region = None
            region_elem = card.locator(".pro-pcr-desc:has(.pro-img-region)").first
            if region_elem.count() > 0:
                region_text = region_elem.inner_text()
                # Извлекаем регион после слова "регион"
                region_match = re.search(r'регион\s*(.+)', region_text)
                if region_match:
                    region = _clean(region_match.group(1))
            
            results.append(
                TenderResult(
                    title=title,
                    url=_normalize_url(url, "https://synapsenet.ru"),
                    source="SYNAPSE",
                    customer=customer,
                    price=price,
                    status=status,
                    law_type=law_type,
                    purchase_type=purchase_type,
                    platform=platform,
                    deadline=deadline,
                    publish_date=publish_date,
                    region=region,
                )
            )
            
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге карточки {i+1}: {e}")
            continue
    
    return results


def collect_page_results_synapse(page: Page) -> List[TenderResult]:
    """Извлечение результатов поиска со страницы synapsenet.ru."""
    results: List[TenderResult] = []

    # Приоритет: специфичная структура Synapse
    synapse_cards = page.locator("div.sp-tender-block")
    
    if synapse_cards.count() > 0:
        print(f"    ✓ Найдена структура Synapse (div.sp-tender-block): {synapse_cards.count()} карточек")
        return parse_synapse_specific_cards(synapse_cards)
    
    # Альтернативные селекторы для карточек тендеров
    possible_selectors = [
        "div.tender-card",
        "div.tender-item",
        ".tender",
        ".purchase",
        "article.tender",
        ".result-item",
        ".search-result",
        "div[class*='tender']",
        "div[class*='purchase']",
        ".card",
    ]
    
    tender_items = None
    for selector in possible_selectors:
        try:
            items = page.locator(selector)
            if items.count() > 0:
                print(f"    ✓ Найдены тендеры с селектором: {selector} ({items.count()} шт.)")
                tender_items = items
                break
        except Exception:
            continue
    
    if tender_items is None or tender_items.count() == 0:
        print("    ⚠️  Тендеры не найдены, пробуем универсальный подход...")
        return collect_page_results_fallback(page)
    
    return parse_synapse_tenders(tender_items)


def collect_page_results_fallback(page: Page) -> List[TenderResult]:
    """Запасной вариант сбора результатов через поиск всех ссылок."""
    results: List[TenderResult] = []
    
    # Ищем все ссылки, которые могут вести на тендеры
    links = page.locator("a[href*='tender'], a[href*='purchase'], a[href*='zakupk']")
    
    seen_urls = set()
    for i in range(min(links.count(), 50)):  # Ограничиваем 50 результатами
        try:
            link = links.nth(i)
            title = link.inner_text(timeout=1_000).strip()
            url = link.get_attribute("href") or ""
            
            if title and url and url not in seen_urls and len(title) > 10:
                seen_urls.add(url)
                results.append(
                    TenderResult(
                        title=_clean(title) or title,
                        url=_normalize_url(url, page.url),
                        source="SYNAPSE",
                    )
                )
        except Exception:
            continue
    
    return results


def go_next_page(page: Page) -> bool:
    """Переход на следующую страницу результатов."""
    next_selectors = [
        "a[rel='next']",
        "button[aria-label='Следующая']",
        "a[aria-label='Следующая']",
        ".pagination__next:not(.disabled)",
        ".pagination .next:not(.disabled)",
        ".next-page:not(.disabled)",
        "a:has-text('Следующая'):not(.disabled)",
        "button:has-text('Следующая'):not(:disabled)",
        "a:has-text('»')",
        "a:has-text('→')",
        ".pager__next a",
        ".pagination li:last-child a",
    ]
    
    for selector in next_selectors:
        try:
            next_elem = page.locator(selector).first
            if next_elem.count() > 0 and next_elem.is_visible(timeout=1_000):
                is_disabled = next_elem.get_attribute("disabled")
                class_attr = next_elem.get_attribute("class") or ""
                
                if is_disabled or "disabled" in class_attr:
                    continue
                    
                next_elem.click()
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(1_500)
                return True
        except Exception:
            continue
    
    return False


# ---------------------------
# Основная логика поиска
# ---------------------------

def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска тендеров на synapsenet.ru."""
    
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
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
            try:
                # Шаг 1: Загружаем главную страницу
                print("Загрузка synapsenet.ru...")
                page.goto("https://synapsenet.ru/", wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                
                # Шаг 2: Ищем поле поиска
                search_box = find_search_input(page)
                
                if search_box is None:
                    # Пробуем страницу поиска
                    alternative_urls = [
                        "https://synapsenet.ru/search",
                        "https://synapsenet.ru/tenders",
                    ]
                    
                    for url in alternative_urls:
                        print(f"  Пробуем {url}...")
                        try:
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(2_000)
                            search_box = find_search_input(page)
                            if search_box:
                                break
                        except Exception:
                            continue

                if search_box:
                    # Шаг 3: Выполняем поиск
                    print(f"\nВыполняем поиск: '{cfg.query}'")
                    search_box.click()
                    search_box.fill(cfg.query)
                    page.wait_for_timeout(500)
                    
                    # Ищем кнопку поиска Synapse
                    search_button_selectors = [
                        "#search-button-script",  # Специфичная кнопка Synapse
                        ".sib-button",
                        "button:has-text('найти тендеры')",
                        "div:has-text('найти тендеры')",
                        "button[type='submit']",
                        "button:has-text('Найти')",
                    ]
                    
                    button_found = False
                    for selector in search_button_selectors:
                        try:
                            search_button = page.locator(selector).first
                            if search_button.count() > 0 and search_button.is_visible(timeout=1_000):
                                print(f"    ✓ Найдена кнопка: {selector}")
                                search_button.click()
                                button_found = True
                                break
                        except Exception:
                            continue
                    
                    if not button_found:
                        print("    Кнопка не найдена, используем Enter...")
                        search_box.press("Enter")
                    
                    # Ждём загрузки результатов
                    print("    Ожидание загрузки результатов...")
                    page.wait_for_load_state("networkidle", timeout=cfg.navigation_timeout)
                    page.wait_for_timeout(3_000)  # Дополнительное ожидание для динамических результатов
                    print("    ✓ Результаты загружены")
                else:
                    print("⚠️  Поле поиска не найдено, пробуем парсить главную страницу...")
            
            except PlaywrightTimeoutError as e:
                raise RuntimeError(f"Таймаут при загрузке страницы: {e}")

            # Таймаут перед парсингом
            print("")
            countdown_timer(cfg.parse_delay // 1000, "⏱️  Таймаут")
            print("")
            
            # Шаг 4: Собираем результаты
            collected: List[TenderResult] = []

            for page_idx in range(cfg.pages):
                print(f"┌{'─' * 58}┐")
                print(f"│ Страница {page_idx + 1}/{cfg.pages}{' ' * (50 - len(f'Страница {page_idx + 1}/{cfg.pages}'))}│")
                print(f"└{'─' * 58}┘")
                
                page_results = collect_page_results_synapse(page)
                collected.extend(page_results)
                print(f"  ✓ Собрано: {len(page_results)} результатов (всего: {len(collected)})")

                if page_idx + 1 >= cfg.pages:
                    break

                print("  → Переход на следующую страницу...")
                if not go_next_page(page):
                    print("  ⚠ Следующая страница не найдена, завершаем.")
                    break
                
                print("")
                countdown_timer(cfg.parse_delay // 1000, "⏱️  Таймаут перед парсингом следующей страницы")
                print("")

            # Шаг 5 (опционально): извлекаем «Позиции» на страницах тендеров
            if cfg.include_positions:
                print("\n" + "=" * 70)
                print("📦 ИЗВЛЕЧЕНИЕ ПОЗИЦИЙ (СОДЕРЖАНИЕ ПОСТАВКИ)")
                print("=" * 70)
                max_t = min(len(collected), max(0, int(cfg.positions_max_tenders)))
                for idx in range(max_t):
                    t = collected[idx]
                    try:
                        print(f"  • {idx + 1}/{max_t}: {t.title[:80]}")
                        positions = fetch_positions(context, t.url, cfg)
                        if positions:
                            t.positions = positions
                            print(f"    ✓ Позиции: {len(positions)}")
                        else:
                            t.positions = []
                            print("    ⚠️ Позиции не найдены")
                    except Exception as e:
                        t.positions = []
                        print(f"    ❌ Ошибка позиций: {e}")

            return collected
        finally:
            context.close()
            browser.close()


def save_results(path: Path, results: Iterable[TenderResult]) -> None:
    """Сохранение результатов в JSON-файл."""
    data = [asdict(res) for res in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------
# CLI
# ---------------------------

def parse_args(argv: List[str]) -> SearchConfig:
    """Парсинг аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Парсер результатов поиска synapsenet.ru через Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python synapse_parser.py "строительство"
  python synapse_parser.py "медицинское оборудование" -p 3
  python synapse_parser.py "IT услуги" --no-headless
        """
    )
    
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1, 
                       help="Количество страниц для парсинга (по умолчанию: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("synapse_results.json"),
                       help="Файл для сохранения результатов (по умолчанию: synapse_results.json)")
    parser.add_argument("--headless", action="store_true", default=True,
                       help="Запуск без интерфейса браузера (по умолчанию)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                       help="Запуск с видимым окном браузера")
    parser.add_argument("--timeout", type=int, default=30_000,
                       help="Таймаут навигации в миллисекундах (по умолчанию: 30000)")
    parser.add_argument("--parse-delay", type=int, default=5,
                       help="Задержка перед парсингом каждой страницы в секундах (по умолчанию: 5)")
    parser.add_argument(
        "--with-positions",
        action="store_true",
        default=False,
        help="Открывать карточки тендеров и извлекать раздел «Позиции» (содержание поставки)",
    )
    parser.add_argument(
        "--positions-limit",
        type=int,
        default=50,
        help="Лимит строк из таблицы «Позиции» на один тендер (по умолчанию: 50)",
    )
    parser.add_argument(
        "--positions-max-tenders",
        type=int,
        default=20,
        help="Лимит тендеров для детализации «Позиции» (по умолчанию: 20)",
    )
    
    args = parser.parse_args(argv)
    
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
        parse_delay=args.parse_delay * 1000,
        include_positions=args.with_positions,
        positions_limit=args.positions_limit,
        positions_max_tenders=args.positions_max_tenders,
    )


def main(argv: List[str]) -> int:
    """Точка входа в программу."""
    cfg = parse_args(argv)
    start = time.time()
    
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 7 + "ПАРСЕР SYNAPSENET.RU (PLAYWRIGHT)" + " " * 17 + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"  Запрос:          {cfg.query}")
    print(f"  Страниц:         {cfg.pages}")
    print(f"  Headless:        {cfg.headless}")
    print(f"  Задержка:        {cfg.parse_delay // 1000} секунд")
    print(f"  Выходной файл:   {cfg.output}")
    print("─" * 60)
    
    try:
        results = run_search(cfg)
    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем")
        return 130
    except Exception as exc:
        print(f"\n[!] Ошибка: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    save_results(cfg.output, results)
    
    duration = time.time() - start
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 20 + "РЕЗУЛЬТАТЫ" + " " * 28 + "║")
    print("╠" + "═" * 58 + "╣")
    print(f"║  ✓ Собрано результатов:  {len(results):<30} ║")
    print(f"║  ✓ Время выполнения:     {duration:.1f} сек{' ' * (28 - len(f'{duration:.1f} сек'))} ║")
    print(f"║  ✓ Сохранено в:          {str(cfg.output)[:28]:<30} ║")
    print("╚" + "═" * 58 + "╝")
    print("")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

