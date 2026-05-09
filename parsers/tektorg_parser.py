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
    output: Path = Path("tektorg_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 5_000  # ms


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "TEKTORG"  # Источник данных
    customer: Optional[str] = None  # Заказчик
    organizer: Optional[str] = None  # Организатор
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None
    deadline: Optional[str] = None  # Срок подачи заявок
    region: Optional[str] = None
    tender_id: Optional[str] = None  # ID/номер процедуры
    law_type: Optional[str] = None  # 44-ФЗ, 223-ФЗ и т.д.
    purchase_type: Optional[str] = None
    platform: Optional[str] = "ТЭК-Торг"  # ЭТП


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
        return "https://www.tektorg.ru" + href
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
    """Поиск поля ввода для поискового запроса на tektorg.ru (текст вводить сюда)."""
    search_selectors = [
        # Основное поле поиска на главной — приоритет
        "input[placeholder='Введите слово или номер процедуры']",
        "input[type='text'][placeholder*='номер процедуры']",
        "input[type='text'][placeholder*='слово или номер']",
        # Специфичные классы tektorg.ru
        "div.sc-69bbd0ab-2.kYnWVd input[type='text']",
        "div.kYnWVd input[type='text']",
        ".sc-69bbd0ab-2 input[type='text']",
        # Общие селекторы
        "input[placeholder*='поиск']",
        "input[placeholder*='ключевое слово']",
        "input[type='text'][name*='search']",
        "input[type='search']",
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
# Сбор результатов со страницы ТЭК-Торг
# ---------------------------

def parse_tektorg_cards(tender_items: Locator) -> List[TenderResult]:
    """
    Парсинг карточек тендеров ТЭК-Торг
    
    Структура (из предоставленного HTML):
    - Контейнер: div.sc-6c01eeae-0.jtfzxc
    - Номер процедуры: span.sc-375e6608-4.eBkrKS (№ 0387200026225000147)
    - Статус: span.sc-3e697cd2-2.iLrznN ("Приём заявок")
    - Название и URL: a.sc-6c01eeae-7.gccepd
    - Организатор: div.sc-6c01eeae-10.hqcmWX
    - Цена: div.sc-a6b34174-0.cLruXa (138 800,04 ₽)
    - Дата публикации: time[datetime] (первая, 2025-12-05T07:22:59+03:00)
    - Дедлайн: time[datetime] (вторая, 2025-12-15T08:00:00+03:00)
    - Тип площадки: текст "Государственные закупки" / "Коммерческие закупки"
    """
    results: List[TenderResult] = []
    
    for i in range(tender_items.count()):
        card = tender_items.nth(i)
        try:
            # Получаем весь текст карточки для fallback
            full_text = card.inner_text()
            
            # Номер процедуры из .sc-375e6608-4 или текста
            tender_id = None
            id_elem = card.locator(".sc-375e6608-4").first
            if id_elem.count() > 0:
                tender_id_text = _clean(id_elem.inner_text(timeout=1_000))
                if tender_id_text:
                    tender_id = tender_id_text.replace("№", "").strip()
            
            # Название и URL из a.sc-6c01eeae-7
            title = None
            url = ""
            title_link = card.locator("a.sc-6c01eeae-7").first
            if title_link.count() > 0:
                title = _clean(title_link.inner_text(timeout=2_000))
                url = title_link.get_attribute("href") or ""
            
            if not title or len(title) < 5:
                continue
            
            # Организатор из .sc-6c01eeae-10
            organizer = None
            org_elem = card.locator(".sc-6c01eeae-10").first
            if org_elem.count() > 0:
                organizer = _clean(org_elem.inner_text(timeout=1_000))
            
            # Цена из .sc-a6b34174-0
            price = None
            price_elem = card.locator(".sc-a6b34174-0").first
            if price_elem.count() > 0:
                price_text = _clean(price_elem.inner_text(timeout=1_000))
                if price_text:
                    price = price_text
            
            # Статус из .sc-3e697cd2-2
            status = None
            status_elem = card.locator(".sc-3e697cd2-2").first
            if status_elem.count() > 0:
                status = _clean(status_elem.inner_text(timeout=1_000))
            
            # Даты из элементов time
            publish_date = None
            deadline = None
            time_elements = card.locator("time[datetime]")
            if time_elements.count() >= 2:
                # Первая дата - публикация
                pub_elem = time_elements.nth(0)
                pub_datetime = pub_elem.get_attribute("datetime")
                if pub_datetime:
                    publish_date = pub_datetime.split("T")[0]
                    publish_date = publish_date.replace("-", ".")
                    # Переформатируем в dd.mm.yyyy
                    parts = publish_date.split(".")
                    if len(parts) == 3:
                        publish_date = f"{parts[2]}.{parts[1]}.{parts[0]}"
                
                # Вторая дата - дедлайн
                deadline_elem = time_elements.nth(1)
                deadline_datetime = deadline_elem.get_attribute("datetime")
                if deadline_datetime:
                    deadline = deadline_datetime.replace("T", " ").split("+")[0]
                    # Форматируем дату
                    date_part = deadline.split(" ")[0]
                    time_part = deadline.split(" ")[1] if " " in deadline else ""
                    parts = date_part.split("-")
                    if len(parts) == 3:
                        deadline = f"{parts[2]}.{parts[1]}.{parts[0]}"
                        if time_part:
                            deadline = f"{deadline} {time_part[:5]}"
            
            # Определяем закон (44-ФЗ, 223-ФЗ, 615-ПП) из текста или типа площадки
            law_type = None
            platform_text = full_text
            law_match = re.search(r'(\d{2,3}-(?:ФЗ|ПП))', platform_text)
            if law_match:
                law_type = law_match.group(1)
            
            # Тип площадки/закупки
            purchase_type = None
            if "Государственные закупки" in full_text:
                purchase_type = "Государственные закупки"
            elif "Интернет-магазин" in full_text:
                purchase_type = "Интернет-магазин"
            elif "Коммерческие закупки" in full_text:
                purchase_type = "Коммерческие закупки"
            
            # Регион - попробуем извлечь из организатора или текста
            region = None
            region_patterns = [
                r'(\d{2}\.\s*[А-Яа-яЁё\s]+(?:область|край|респ|город))',
                r'([А-ЯЁ][а-яё]+(?:ская|ский|ское)\s+область)',
                r'([А-ЯЁ][а-яё]+(?:ский|ская)\s+край)',
            ]
            for pattern in region_patterns:
                match = re.search(pattern, full_text)
                if match:
                    region = _clean(match.group(1))
                    break
            
            # Фильтруем старые тендеры (2021-2023)
            if deadline and any(year in str(deadline) for year in ['2021', '2022', '2023']):
                continue
            if publish_date and any(year in str(publish_date) for year in ['2021', '2022', '2023']):
                continue
            
            results.append(
                TenderResult(
                    title=title,
                    url=_normalize_url(url, "https://www.tektorg.ru"),
                    source="TEKTORG",
                    tender_id=tender_id,
                    customer=None,  # На ТЭК-Торг используется "Организатор"
                    organizer=organizer,
                    price=price,
                    law_type=law_type,
                    purchase_type=purchase_type,
                    status=status,
                    publish_date=publish_date,
                    deadline=deadline,
                    region=region,
                    platform="ТЭК-Торг",
                )
            )
            
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге карточки {i+1}: {e}")
            continue
    
    return results


def collect_page_results_tektorg(page: Page) -> List[TenderResult]:
    """Извлечение результатов поиска со страницы tektorg.ru."""
    results: List[TenderResult] = []

    # Возможные селекторы для карточек тендеров (из HTML структуры)
    possible_selectors = [
        ".sc-6c01eeae-0.jtfzxc",  # Основной селектор из HTML
        "div.jtfzxc",
        ".sc-6c01eeae-0",
        ".procedure-card",
        ".tender-card",
        ".lot-card",
        "[class*='card']",
        ".procedure-item",
        ".tender-item",
        ".search-result",
        ".result-item",
        "article",
        "[data-testid*='card']",
        "[class*='Item']",
    ]
    
    tender_items = None
    for selector in possible_selectors:
        try:
            items = page.locator(selector)
            if items.count() > 0:
                print(f"    ✓ Найдены процедуры с селектором: {selector} ({items.count()} шт.)")
                tender_items = items
                break
        except Exception:
            continue
    
    if tender_items is None or tender_items.count() == 0:
        print("    ⚠️  Процедуры не найдены, пробуем универсальный подход...")
        return collect_page_results_fallback(page)
    
    return parse_tektorg_cards(tender_items)


def collect_page_results_fallback(page: Page) -> List[TenderResult]:
    """Запасной вариант сбора результатов через поиск всех ссылок."""
    results: List[TenderResult] = []
    
    # Ищем все ссылки, которые могут вести на процедуры
    links = page.locator("a[href*='procedure'], a[href*='tender'], a[href*='lot']")
    
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
                        source="TEKTORG",
                    )
                )
        except Exception:
            continue
    
    return results


def go_next_page(page: Page) -> bool:
    """Переход на следующую страницу результатов."""
    next_selectors = [
        "a[rel='next']",
        "button[aria-label*='Следующая']",
        "a[aria-label*='Следующая']",
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
    """Основная функция поиска процедур на tektorg.ru (ввод в поле «Введите слово или номер процедуры»)."""
    
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")
    
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
            # Шаг 1: Загружаем главную страницу
            print("Загрузка https://www.tektorg.ru/...")
            page.goto("https://www.tektorg.ru/", wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(3_000)
            
            # Шаг 2: Ищем поле поиска (placeholder «Введите слово или номер процедуры»)
            search_box = find_search_input(page)
            
            if search_box is None:
                alternative_urls = [
                    "https://www.tektorg.ru/ru/procedures",
                    "https://www.tektorg.ru/search",
                    "https://www.tektorg.ru/procedures",
                ]
                for url in alternative_urls:
                    print(f"  Пробуем {url}...")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                        page.wait_for_timeout(2_000)
                        search_box = find_search_input(page)
                        if search_box:
                            break
                    except Exception:
                        continue

            if search_box:
                # Шаг 3: Ввод запроса в поле поиска и отправка
                print(f"\nВвод запроса в поле поиска: '{cfg.query}'")
                search_box.click()
                search_box.fill(cfg.query)
                page.wait_for_timeout(600)
                # Кнопка «Найти» или Enter (сайт подгружает procedures.json?name=...)
                search_button_selectors = [
                    "div.sc-69bbd0ab-3.eHyHtU button",
                    "div.eHyHtU button",
                    ".sc-69bbd0ab-3 button",
                    "button.sc-69bbd0ab-8.cbuMSI",
                    "button.cbuMSI",
                    "button:has-text('Найти')",
                    "button[type='submit']",
                    "button:has-text('Искать')",
                ]
                button_found = False
                for btn_selector in search_button_selectors:
                    try:
                        btn = page.locator(btn_selector).first
                        if btn.count() > 0 and btn.is_visible(timeout=1_000):
                            btn.click()
                            button_found = True
                            break
                    except Exception:
                        continue
                if not button_found:
                    search_box.press("Enter")
                print("    Ожидание загрузки результатов...")
                page.wait_for_load_state("domcontentloaded", timeout=cfg.navigation_timeout)
                page.wait_for_timeout(3_000)
                print("    ✓ Результаты загружены")
            else:
                print("⚠️  Поле поиска не найдено, парсим текущую страницу...")
            
        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"Таймаут при загрузке страницы: {e}")

        try:
            # Таймаут перед парсингом
            print("")
            countdown_timer(cfg.parse_delay // 1000, "⏱️  Таймаут")
            print("")
            
            # Шаг 4: Собираем результаты
            collected: List[TenderResult] = []

            for page_idx in range(cfg.pages):
                page_info = f"Страница {page_idx + 1}/{cfg.pages}"
                print(f"┌{'─' * 58}┐")
                print(f"│ {page_info}{' ' * (56 - len(page_info))}│")
                print(f"└{'─' * 58}┘")
                
                page_results = collect_page_results_tektorg(page)
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
        description="Парсер результатов поиска ТЭК-Торг (tektorg.ru). Поиск: поле «Введите слово или номер процедуры».",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python tektorg_parser.py "строительство"
  python tektorg_parser.py "медицинское оборудование" -p 3
  python tektorg_parser.py "поставка компьютеров" --no-headless
        """
    )
    
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1, 
                       help="Количество страниц для парсинга (по умолчанию: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("tektorg_results.json"),
                       help="Файл для сохранения результатов (по умолчанию: tektorg_results.json)")
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
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 12 + "ПАРСЕР ТЭК-ТОРГ (tektorg.ru)" + " " * 19 + "║")
    print("╚" + "=" * 58 + "╝")
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
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 20 + "РЕЗУЛЬТАТЫ" + " " * 28 + "║")
    print("╠" + "=" * 58 + "╣")
    print(f"║  ✓ Собрано результатов:  {len(results):<30} ║")
    print(f"║  ✓ Время выполнения:     {duration:.1f} сек{' ' * (28 - len(f'{duration:.1f} сек'))} ║")
    print(f"║  ✓ Сохранено в:          {str(cfg.output)[:28]:<30} ║")
    print("╚" + "=" * 58 + "╝")
    print("")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

