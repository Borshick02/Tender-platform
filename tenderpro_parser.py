from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Locator


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("tenderpro_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 5_000  # ms


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "TENDER.PRO"  # Источник данных
    customer: Optional[str] = None  # Заказчик/Организация
    organizer: Optional[str] = None
    price: Optional[str] = None
    status: Optional[str] = None  # Открыт, Завершен и т.д.
    publish_date: Optional[str] = None
    deadline: Optional[str] = None  # Срок завершения
    region: Optional[str] = None
    tender_id: Optional[str] = None  # ID тендера
    purchase_type: Optional[str] = None


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
        return "https://www.tender.pro" + href
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
    """Поиск поля ввода для поискового запроса на tender.pro."""
    search_selectors = [
        # Специфичные для tender.pro (точное совпадение)
        "input[name='good_name']",
        "input.input__inp._search",
        "input[placeholder*='Искать конкурсы']",
        # Общие селекторы
        "input[name='search']",
        "input[placeholder*='поиск']",
        "input[placeholder*='Поиск']",
        "input[name='q']",
        "input[type='search']",
        "input[type='text']",
        "#search",
        ".search-input",
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
# Сбор результатов со страницы Tender.Pro
# ---------------------------

def parse_tenderpro_table(table_rows: Locator) -> List[TenderResult]:
    """
    Парсинг таблицы тендеров Tender.Pro
    
    Структура таблицы (tr.table-stat__row):
    - td.tender__id - ID тендера
    - td.tender__name - Название с ссылкой
    - Дата создания (Создан)
    - td.tender__untill - Прием до
    - td.tender__close-date - Закрыт
    - td.tender__status - Статус (иконка)
    - td.tender__company - Компания
    """
    results: List[TenderResult] = []
    
    for i in range(table_rows.count()):
        row = table_rows.nth(i)
        try:
            # Пропускаем заголовок таблицы
            if row.locator("th").count() > 0:
                continue
            
            # ID тендера
            tender_id = None
            id_elem = row.locator("td.tender__id").first
            if id_elem.count() > 0:
                tender_id = _clean(id_elem.inner_text(timeout=1_000))
            
            # Название и URL
            title = None
            url = ""
            name_cell = row.locator("td.tender__name").first
            if name_cell.count() > 0:
                # Ищем ссылку в ячейке
                link = name_cell.locator("a[href*='/api/tender/']").first
                if link.count() > 0:
                    title_text = link.inner_text(timeout=2_000)
                    url = link.get_attribute("href") or ""
                    
                    # Очищаем заголовок от (idXXXXXX)
                    title = re.sub(r'\s*\(id\d+\)\s*$', '', title_text.strip())
            
            if not title or len(title) < 5:
                continue
            
            # Дата создания (3-я колонка)
            publish_date = None
            date_cells = row.locator("td").all()
            if len(date_cells) >= 3:
                publish_date = _clean(date_cells[2].inner_text(timeout=1_000))
            
            # Прием до
            deadline = None
            deadline_elem = row.locator("td.tender__untill").first
            if deadline_elem.count() > 0:
                deadline = _clean(deadline_elem.inner_text(timeout=1_000))
            
            # Дата закрытия
            close_date = None
            close_elem = row.locator("td.tender__close-date").first
            if close_elem.count() > 0:
                close_date = _clean(close_elem.inner_text(timeout=1_000))
            
            # Статус (через title иконки или текст)
            status = None
            status_elem = row.locator("td.tender__status img").first
            if status_elem.count() > 0:
                status_title = status_elem.get_attribute("title")
                if status_title:
                    status = status_title
            
            # Если статус не найден, пробуем текстовый вариант
            if not status:
                status_cell = row.locator("td.tender__status").first
                if status_cell.count() > 0:
                    status_text = _clean(status_cell.inner_text(timeout=1_000))
                    if status_text:
                        status = status_text
            
            # Компания (заказчик)
            customer = None
            company_elem = row.locator("td.tender__company a").first
            if company_elem.count() > 0:
                customer = _clean(company_elem.inner_text(timeout=1_000))
            
            # Фильтруем старые тендеры (2021-2023)
            if deadline and any(year in str(deadline) for year in ['2021', '2022', '2023']):
                continue
            if publish_date and any(year in str(publish_date) for year in ['2021', '2022', '2023']):
                continue
            
            results.append(
                TenderResult(
                    title=_clean(title) or title,
                    url=_normalize_url(url, "https://www.tender.pro"),
                    source="TENDER.PRO",
                    tender_id=tender_id,
                    customer=customer,
                    status=status,
                    publish_date=publish_date,
                    deadline=deadline,
                )
            )
            
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге строки {i+1}: {e}")
            continue
    
    return results


def parse_tenderpro_cards(tender_items: Locator) -> List[TenderResult]:
    """
    Парсинг карточек тендеров Tender.Pro (запасной вариант)
    """
    results: List[TenderResult] = []
    
    for i in range(tender_items.count()):
        card = tender_items.nth(i)
        try:
            # Получаем весь текст карточки
            full_text = card.inner_text()
            
            # ID тендера
            tender_id = None
            id_match = re.search(r'id(\d+)', full_text)
            if id_match:
                tender_id = id_match.group(1)
            else:
                number_match = re.search(r'\b(\d{6,})\b', full_text)
                if number_match:
                    tender_id = number_match.group(1)
            
            # Название тендера (ищем в ссылках)
            title = None
            url = ""
            
            link_elem = card.locator("a").first
            if link_elem.count() > 0:
                title = link_elem.inner_text(timeout=2_000).strip()
                url = link_elem.get_attribute("href") or ""
                title = re.sub(r'\s*\(id\d+\)\s*$', '', title)
            
            if not title or len(title) < 5:
                continue
            
            # Срок завершения
            deadline = None
            deadline_patterns = [
                r'Прием заявок[:\s]+(\d{2}\.\d{2}\.\d{4}[^\d]*\d{2}:\d{2})',
                r'(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})',
                r'(\d{2}\.\d{2}\.\d{4})',
            ]
            for pattern in deadline_patterns:
                match = re.search(pattern, full_text)
                if match:
                    deadline = match.group(1)
                    break
            
            # Статус
            status = None
            if 'Открыта процедура' in full_text:
                status = 'Открыта процедура'
            elif 'Закрыт' in full_text:
                status = 'Закрыт'
            
            # Компания
            customer = None
            company_match = re.search(r'Компания[:\s]+([^\n]+)', full_text)
            if company_match:
                customer = _clean(company_match.group(1))
            
            results.append(
                TenderResult(
                    title=_clean(title) or title.strip(),
                    url=_normalize_url(url, "https://www.tender.pro"),
                    source="TENDER.PRO",
                    customer=customer,
                    status=status,
                    tender_id=tender_id,
                    deadline=deadline,
                )
            )
            
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге карточки {i+1}: {e}")
            continue
    
    return results


def collect_page_results_tenderpro(page: Page) -> List[TenderResult]:
    """Извлечение результатов поиска со страницы tender.pro."""
    results: List[TenderResult] = []

    # Сначала пробуем найти таблицу (основной формат)
    try:
        table_rows = page.locator("table.table-stat tr.table-stat__row")
        if table_rows.count() > 0:
            print(f"    ✓ Найдена таблица с тендерами: {table_rows.count()} строк")
            return parse_tenderpro_table(table_rows)
    except Exception as e:
        print(f"    ⚠️  Таблица не найдена: {e}")
    
    # Если таблицы нет, ищем карточки
    possible_selectors = [
        "div.tender-card",
        "div.tender-item",
        ".tender",
        ".competition",
        ".auction",
        "article",
        ".result-item",
        ".card",
        "div[class*='tender']",
        "div[class*='competition']",
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
    
    return parse_tenderpro_cards(tender_items)


def collect_page_results_fallback(page: Page) -> List[TenderResult]:
    """Запасной вариант сбора результатов через поиск всех ссылок."""
    results: List[TenderResult] = []
    
    # Ищем все ссылки, которые могут вести на тендеры
    links = page.locator("a[href*='tender'], a[href*='competition'], a[href*='auction']")
    
    seen_urls = set()
    for i in range(min(links.count(), 50)):  # Ограничиваем 50 результатами
        try:
            link = links.nth(i)
            title = link.inner_text(timeout=1_000).strip()
            url = link.get_attribute("href") or ""
            
            if title and url and url not in seen_urls and len(title) > 10:
                seen_urls.add(url)
                
                # Очищаем заголовок от ID
                title = re.sub(r'\s*\(id\d+\)\s*$', '', title)
                
                results.append(
                    TenderResult(
                        title=_clean(title) or title,
                        url=_normalize_url(url, page.url),
                        source="TENDER.PRO",
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
    """Основная функция поиска тендеров на tender.pro."""
    
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
                print("Загрузка tender.pro...")
                page.goto("https://www.tender.pro/", wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                
                # Шаг 2: Ищем поле поиска
                search_box = find_search_input(page)
                
                if search_box is None:
                    # Пробуем страницу поиска/тендеров
                    alternative_urls = [
                        "https://www.tender.pro/search",
                        "https://www.tender.pro/tenders",
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
                    
                    # Ищем кнопку поиска (tender.pro использует button.search-btn)
                    search_button_selectors = [
                        "button.search-btn",
                        "button[type='submit']",
                        "button:has-text('Найти')",
                        "button:has-text('Поиск')",
                    ]
                    
                    button_found = False
                    for btn_selector in search_button_selectors:
                        try:
                            btn = page.locator(btn_selector).first
                            if btn.count() > 0 and btn.is_visible(timeout=1_000):
                                print(f"    Нажатие кнопки поиска ({btn_selector})...")
                                btn.click()
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
                
                page_results = collect_page_results_tenderpro(page)
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
        description="Парсер результатов поиска tender.pro через Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python tenderpro_parser.py "строительство"
  python tenderpro_parser.py "металлолом" -p 3
  python tenderpro_parser.py "спецтехника" --no-headless
        """
    )
    
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1, 
                       help="Количество страниц для парсинга (по умолчанию: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("tenderpro_results.json"),
                       help="Файл для сохранения результатов (по умолчанию: tenderpro_results.json)")
    parser.add_argument("--headless", action="store_true", default=True,
                       help="Запуск без интерфейса браузера (по умолчанию)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                       help="Запуск с видимым окном браузера")
    parser.add_argument("--timeout", type=int, default=30_000,
                       help="Таймаут навигации в миллисекундах (по умолчанию: 30000)")
    parser.add_argument("--parse-delay", type=int, default=5,
                       help="Задержка перед парсингом каждой страницы в секундах (по умолчанию: 5)")
    
    args = parser.parse_args(argv)
    
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
        parse_delay=args.parse_delay * 1000,
    )


def main(argv: List[str]) -> int:
    """Точка входа в программу."""
    cfg = parse_args(argv)
    start = time.time()
    
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "ПАРСЕР TENDER.PRO (PLAYWRIGHT)" + " " * 18 + "║")
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

