#!/usr/bin/env python3
"""
Парсер goszakup.gov.kz — Портал государственных закупок Республики Казахстан.

HTTP-only (без Playwright), очень быстрый.
Поиск идёт через `/ru/search/announce?filter[name]=<запрос>`,
парсим таблицу `#search-result` (7 колонок).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://goszakup.gov.kz"
SEARCH_PATH = "/ru/search/announce"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("goszakup_kz_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000
    parse_delay: int = 0


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "GOSZAKUP-KZ"
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


def _clean(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    out = " ".join(s.split()).strip()
    return out or None


def _build_url(query: str, page: int = 1) -> str:
    params = {"filter[name]": query}
    if page > 1:
        params["page"] = str(page)
    return f"{BASE_URL}{SEARCH_PATH}?{urllib.parse.urlencode(params)}"


def _split_title_organizer(text: str) -> tuple[str, Optional[str]]:
    """Текст 2-й колонки: 'Название Организатор: ИЮЛ'. Делим по ключевому маркеру."""
    if not text:
        return "", None
    m = re.search(r"\bОрганизатор[:\s]+(.+)$", text, re.I)
    if not m:
        return text.strip(), None
    title = text[: m.start()].strip().rstrip(":").strip()
    organizer = m.group(1).strip()
    return title, organizer or None


def _extract_id_lots(text: str) -> tuple[Optional[str], Optional[str]]:
    """Текст 1-й колонки: '16959495-1 Лотов: 1'. Возвращает (announce_id, lots_count_str)."""
    if not text:
        return None, None
    parts = text.split()
    aid = None
    lots = None
    for p in parts:
        if re.match(r"^\d{6,}", p):
            aid = p
            break
    m = re.search(r"Лотов?:\s*(\d+)", text, re.I)
    if m:
        lots = m.group(1)
    return aid, lots


def _normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = _clean(text)
    if not cleaned:
        return None
    if re.search(r"[₸|тг|тенге]", cleaned, re.I):
        return cleaned
    if re.search(r"\d", cleaned):
        return f"{cleaned} ₸"
    return cleaned


def _parse_search_html(html: str) -> List[TenderResult]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#search-result") or soup.select_one("table.dataTable")
    if not table:
        return []

    rows: List[TenderResult] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        link = tr.select_one("a[href*='/announce/index/']")
        if not link:
            continue

        href = link.get("href") or ""
        url = href if href.startswith("http") else BASE_URL + href

        col0 = _clean(tds[0].get_text(" ", strip=True)) or ""
        col1 = _clean(tds[1].get_text(" ", strip=True)) or ""
        col2 = _clean(tds[2].get_text(" ", strip=True)) if len(tds) > 2 else None
        col3 = _clean(tds[3].get_text(" ", strip=True)) if len(tds) > 3 else None
        col4 = _clean(tds[4].get_text(" ", strip=True)) if len(tds) > 4 else None
        col5 = _clean(tds[5].get_text(" ", strip=True)) if len(tds) > 5 else None
        col6 = _clean(tds[6].get_text(" ", strip=True)) if len(tds) > 6 else None

        announce_id, _lots = _extract_id_lots(col0)
        title, organizer = _split_title_organizer(col1)
        if not title:
            title = link.get_text(" ", strip=True) or "—"

        rows.append(TenderResult(
            title=title,
            url=url,
            customer=organizer,
            organizer=organizer,
            purchase_type=col2,
            publish_date=col3,
            deadline=col4,
            price=_normalize_price(col5),
            status=col6,
            tender_id=announce_id,
            region="Казахстан",
            law_type="РК (госзакупки)",
        ))
    return rows


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция: HTTP GET → парсинг таблицы. Очень быстрый.
    cfg.headless игнорируется (нет браузера)."""
    print(f"goszakup.gov.kz: '{cfg.query}', страниц до {cfg.pages}", file=sys.stderr)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    timeout_s = max(5, int(cfg.navigation_timeout / 1000))

    all_results: List[TenderResult] = []
    seen_urls: set[str] = set()

    for page_num in range(1, max(1, cfg.pages) + 1):
        url = _build_url(cfg.query, page_num)
        try:
            resp = session.get(url, timeout=timeout_s)
        except requests.RequestException as e:
            print(f"⚠ goszakup.gov.kz request error (page {page_num}): {e}", file=sys.stderr)
            break

        if resp.status_code != 200:
            print(f"⚠ goszakup.gov.kz HTTP {resp.status_code} (page {page_num})", file=sys.stderr)
            break

        page_results = _parse_search_html(resp.text)
        if not page_results:
            break

        new_count = 0
        for r in page_results:
            if r.url not in seen_urls:
                all_results.append(r)
                seen_urls.add(r.url)
                new_count += 1

        if new_count == 0:
            break

    print(f"goszakup.gov.kz: собрано {len(all_results)}", file=sys.stderr)
    return all_results


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер goszakup.gov.kz по ключевым словам")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("goszakup_kz_results.json"))
    parser.add_argument("--timeout", type=int, default=30_000)
    args = parser.parse_args(argv)
    return SearchConfig(query=args.query, pages=args.pages, output=args.output,
                        navigation_timeout=args.timeout)


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
