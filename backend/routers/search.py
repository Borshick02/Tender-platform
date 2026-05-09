"""API поиска: запуск парсера, сохранение в БД, логи."""
import io
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from database import SessionLocal
from models import Tender
from ml_service import _parse_price, enrich_tender

router = APIRouter(prefix="/api/search", tags=["search"])

_search_tasks = {}
_search_counter = 0


class SearchRequest(BaseModel):
    query: str
    pages: int = 1
    sources: list[str] = ["rts", "rutend"]


class _TeeOutput(io.TextIOBase):
    """Перехватывает stdout/stderr и копирует в список логов задачи."""

    def __init__(self, original, logs: list):
        self._original = original
        self._logs = logs

    def write(self, text):
        if text and text.strip():
            self._logs.append(text.rstrip())
        if self._original:
            self._original.write(text)
        return len(text) if text else 0

    def flush(self):
        if self._original:
            self._original.flush()


def _run_parser(task_id: str, query: str, pages: int, sources: list):
    """Фоновый запуск парсера и сохранение в БД."""
    task = _search_tasks.get(task_id)
    if not task:
        return
    task["status"] = "running"
    task["progress"] = 0
    logs = task["logs"]

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeOutput(old_stdout, logs)
    sys.stderr = _TeeOutput(old_stderr, logs)

    try:
        try:
            from multi_parser import run_multi_search, MultiSearchConfig
        except ImportError:
            run_multi_search = None
        if run_multi_search is None:
            raise ImportError("multi_parser недоступен")

        logs.append(f"[Pro] Запуск парсинга: '{query}', страниц: {pages}, источники: {sources}")
        task["progress"] = 5

        cfg = MultiSearchConfig(
            query=query,
            pages=pages,
            output=Path(f"results_pro_{task_id}.json"),
            headless=True,
            sources=sources,
            parallel=False,
        )
        results = run_multi_search(cfg)
        raw = results.get("combined", [])
        task["progress"] = 90

        logs.append(f"[Pro] Парсинг завершён, найдено {len(raw)} результатов. Сохраняем в БД...")

        db = SessionLocal()
        try:
            for r in raw:
                price_num = _parse_price(r.get("price", ""))
                t = Tender(
                    tender_id=r.get("tender_id"),
                    title=r.get("title", ""),
                    url=r.get("url"),
                    source=r.get("source"),
                    price_raw=r.get("price"),
                    price_numeric=price_num,
                    customer=r.get("customer"),
                    organizer=r.get("organizer"),
                    law_type=r.get("law_type"),
                    purchase_type=r.get("purchase_type"),
                    deadline=r.get("deadline"),
                    status=r.get("status"),
                    region=r.get("region"),
                    platform=r.get("platform"),
                    publish_date=r.get("publish_date"),
                    search_query=query,
                )
                db.add(t)
            db.commit()
            task["total"] = len(raw)
            logs.append(f"[Pro] Сохранено {len(raw)} тендеров в PostgreSQL")
        except Exception as e:
            task["error"] = str(e)
            logs.append(f"[Pro] Ошибка сохранения в БД: {e}")
        finally:
            db.close()
        task["status"] = "completed"
        task["progress"] = 100
        task["results"] = raw
    except ImportError:
        logs.append("[Pro] multi_parser недоступен — демо-режим")
        demo = [
            {"title": f"Демо {query} 1", "url": "https://example.com/1", "source": "RTS-TENDER",
             "price": "100000 \u20BD", "customer": "ООО Демо", "law_type": "44-ФЗ", "deadline": "01.03.2026"},
            {"title": f"Демо {query} 2", "url": "https://example.com/2", "source": "RUTEND",
             "price": "250000 \u20BD", "customer": "ГБУ Заказчик", "law_type": "223-ФЗ", "deadline": "15.03.2026"},
            {"title": f"Демо {query} 3", "url": "https://example.com/3", "source": "B2B-CENTER",
             "price": "500000 \u20BD", "customer": "АО Тест", "law_type": "223-ФЗ", "deadline": "20.03.2026"},
        ]
        db = SessionLocal()
        try:
            for r in demo:
                price_num = _parse_price(r.get("price", ""))
                t = Tender(
                    title=r.get("title", ""),
                    url=r.get("url"),
                    source=r.get("source"),
                    price_raw=r.get("price"),
                    price_numeric=price_num,
                    customer=r.get("customer"),
                    law_type=r.get("law_type"),
                    deadline=r.get("deadline"),
                    search_query=query,
                )
                db.add(t)
            db.commit()
        finally:
            db.close()
        task["status"] = "completed"
        task["progress"] = 100
        task["results"] = demo
        task["total"] = len(demo)
    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)
        logs.append(f"[Pro] Критическая ошибка: {e}")
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


@router.post("")
def start_search(req: SearchRequest, background_tasks: BackgroundTasks):
    """Запуск поиска. Возвращает task_id."""
    global _search_counter
    _search_counter += 1
    task_id = f"pro_{_search_counter}"
    _search_tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "results": [],
        "total": 0,
        "error": None,
        "logs": [],
    }
    t = threading.Thread(
        target=_run_parser,
        args=(task_id, req.query, req.pages, req.sources),
        daemon=True,
    )
    t.start()
    return {"task_id": task_id}


@router.get("/{task_id}")
def get_search_status(task_id: str):
    """Статус и результаты поиска."""
    task = _search_tasks.get(task_id)
    if not task:
        return {"error": "Задача не найдена"}
    out = {
        "task_id": task_id,
        "status": task["status"],
        "progress": task.get("progress", 0),
        "total_results": task.get("total", 0),
        "results": task.get("results", []),
        "error": task.get("error"),
        "logs": task.get("logs", [])[-100:],
    }
    for r in out["results"]:
        enrich_tender(r)
    return out
