"""API поиска: заявки, привязка к пользователю, хранение в БД."""
import json
import sys
import threading
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

MSK = timezone(timedelta(hours=3))

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from database import SessionLocal, get_db
from models import Tender, SearchRequest as SearchRequestModel, User
from ml_service import _parse_price, enrich_tender
from routers.users import get_current_user

router = APIRouter(prefix="/api", tags=["search"])

MAX_ACTIVE_REQUESTS = 5


class SearchInput(BaseModel):
    query: str
    pages: int = 1
    sources: list[str] = ["rts", "rutend"]
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    min_days_left: Optional[int] = None


_DATE_PATTERNS = [
    (r'(\d{2})\.(\d{2})\.(\d{4})', '%d.%m.%Y'),
    (r'(\d{4})-(\d{2})-(\d{2})', '%Y-%m-%d'),
    (r'(\d{2})/(\d{2})/(\d{4})', '%d/%m/%Y'),
]


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    s = date_str.strip()
    for pattern, fmt in _DATE_PATTERNS:
        m = re.search(pattern, s)
        if m:
            try:
                return datetime.strptime(m.group(0), fmt)
            except ValueError:
                continue
    return None


def _filter_by_dates(tenders: list, date_from: Optional[str], date_to: Optional[str],
                     min_days_left: Optional[int]) -> list:
    if not date_from and not date_to and not min_days_left:
        return tenders

    dt_from = _parse_date(date_from) if date_from else None
    dt_to = _parse_date(date_to) if date_to else None
    now = datetime.now()
    min_deadline = now + timedelta(days=min_days_left) if min_days_left and min_days_left > 0 else None

    filtered = []
    for t in tenders:
        deadline_dt = _parse_date(t.get("deadline") or "")
        publish_dt = _parse_date(t.get("publish_date") or "")

        if dt_from and deadline_dt and deadline_dt < dt_from:
            continue
        if dt_to and publish_dt and publish_dt > dt_to:
            continue
        if min_deadline:
            if not deadline_dt or deadline_dt < min_deadline:
                continue

        filtered.append(t)
    return filtered


def _run_parser_background(request_id: int):
    """Фоновый парсинг — привязка к SearchRequest в БД."""
    db = SessionLocal()
    try:
        sr = db.query(SearchRequestModel).filter(SearchRequestModel.id == request_id).first()
        if not sr:
            return
        sr.status = "running"
        sr.progress = 5
        db.commit()

        logs = []

        try:
            from multi_parser import run_multi_search, MultiSearchConfig
        except ImportError:
            run_multi_search = None

        if run_multi_search is None:
            logs.append("[Pro] multi_parser недоступен — демо-режим")
            demo = [
                {"title": f"Демо {sr.query} 1", "url": "https://example.com/1",
                 "source": "RTS-TENDER", "price": "100000 ₽", "customer": "ООО Демо",
                 "law_type": "44-ФЗ", "deadline": "01.06.2026", "publish_date": "01.01.2026"},
                {"title": f"Демо {sr.query} 2", "url": "https://example.com/2",
                 "source": "RUTEND", "price": "250000 ₽", "customer": "ГБУ Заказчик",
                 "law_type": "223-ФЗ", "deadline": "15.06.2026", "publish_date": "10.01.2026"},
                {"title": f"Демо {sr.query} 3", "url": "https://example.com/3",
                 "source": "B2B-CENTER", "price": "500000 ₽", "customer": "АО Тест",
                 "law_type": "223-ФЗ", "deadline": "20.06.2026", "publish_date": "15.01.2026"},
            ]
            if sr.date_from or sr.date_to or sr.min_days_left:
                demo = _filter_by_dates(demo, sr.date_from, sr.date_to, sr.min_days_left)

            for r in demo:
                price_num = _parse_price(r.get("price", ""))
                t = Tender(
                    title=r.get("title", ""), url=r.get("url"), source=r.get("source"),
                    price_raw=r.get("price"), price_numeric=price_num,
                    customer=r.get("customer"), law_type=r.get("law_type"),
                    deadline=r.get("deadline"), publish_date=r.get("publish_date"),
                    search_query=sr.query, request_id=sr.id,
                )
                db.add(t)
            db.commit()

            sr.status = "completed"
            sr.progress = 100
            sr.total_results = len(demo)
            sr.finished_at = datetime.now(MSK)
            logs.append(f"[Pro] Демо завершён, {len(demo)} результатов")
            sr.logs = logs
            db.commit()
            return

        logs.append(f"[Pro] Запуск парсинга: '{sr.query}', страниц: {sr.pages}, источники: {sr.sources}")
        sr.progress = 10
        sr.logs = logs
        db.commit()

        cfg = MultiSearchConfig(
            query=sr.query,
            pages=sr.pages,
            output=Path(f"results_pro_{sr.id}.json"),
            headless=True,
            sources=sr.sources,
            parallel=False,
        )
        results = run_multi_search(cfg)
        raw = results.get("combined", [])

        sr.progress = 80
        logs.append(f"[Pro] Парсинг завершён, найдено {len(raw)} результатов")
        sr.logs = logs
        db.commit()

        if sr.date_from or sr.date_to or sr.min_days_left:
            before = len(raw)
            raw = _filter_by_dates(raw, sr.date_from, sr.date_to, sr.min_days_left)
            logs.append(f"[Pro] Фильтр по дате: {before} → {len(raw)}")

        sr.progress = 90
        sr.logs = logs
        db.commit()

        for r in raw:
            price_num = _parse_price(r.get("price", ""))
            t = Tender(
                tender_id=r.get("tender_id"), title=r.get("title", ""), url=r.get("url"),
                source=r.get("source"), price_raw=r.get("price"), price_numeric=price_num,
                customer=r.get("customer"), organizer=r.get("organizer"),
                law_type=r.get("law_type"), purchase_type=r.get("purchase_type"),
                deadline=r.get("deadline"), status=r.get("status"),
                region=r.get("region"), platform=r.get("platform"),
                publish_date=r.get("publish_date"), search_query=sr.query,
                request_id=sr.id,
            )
            db.add(t)
        db.commit()

        sr.status = "completed"
        sr.progress = 100
        sr.total_results = len(raw)
        sr.finished_at = datetime.now(MSK)
        logs.append(f"[Pro] Сохранено {len(raw)} тендеров в БД")
        sr.logs = logs
        db.commit()

    except Exception as e:
        import traceback
        sr.status = "error"
        sr.error = str(e)
        sr.finished_at = datetime.now(MSK)
        logs.append(f"[Pro] Ошибка: {e}")
        logs.append(traceback.format_exc())
        sr.logs = logs
        db.commit()
    finally:
        db.close()


@router.post("/search")
def create_search(req: SearchInput,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Создать заявку на поиск. Макс 5 активных одновременно."""
    active_count = db.query(SearchRequestModel).filter(
        SearchRequestModel.user_id == current_user.id,
        SearchRequestModel.status.in_(["pending", "running"])
    ).count()

    if active_count >= MAX_ACTIVE_REQUESTS:
        raise HTTPException(
            429,
            f"Лимит: максимум {MAX_ACTIVE_REQUESTS} активных заявок одновременно. "
            f"Дождитесь завершения текущих."
        )

    sr = SearchRequestModel(
        user_id=current_user.id,
        query=req.query.strip(),
        pages=min(max(req.pages, 1), 10),
        date_from=req.date_from,
        date_to=req.date_to,
        min_days_left=req.min_days_left,
        status="pending",
    )
    sr.sources = req.sources
    db.add(sr)
    db.commit()
    db.refresh(sr)

    t = threading.Thread(target=_run_parser_background, args=(sr.id,), daemon=True)
    t.start()

    return {"request_id": sr.id, "status": "pending",
            "message": "Поиск начался, результат будет в заявках"}


@router.get("/requests")
def list_requests(current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Список всех заявок текущего пользователя."""
    items = db.query(SearchRequestModel).filter(
        SearchRequestModel.user_id == current_user.id
    ).order_by(SearchRequestModel.id.desc()).limit(50).all()
    return {"requests": [r.to_dict() for r in items]}


@router.get("/requests/{request_id}")
def get_request(request_id: int,
                current_user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Статус и результаты конкретной заявки."""
    sr = db.query(SearchRequestModel).filter(
        SearchRequestModel.id == request_id,
        SearchRequestModel.user_id == current_user.id,
    ).first()
    if not sr:
        raise HTTPException(404, "Заявка не найдена")

    data = sr.to_dict(include_results=True)
    for r in data.get("results", []):
        enrich_tender(r)
    return data


@router.delete("/requests/{request_id}")
def delete_request(request_id: int,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Удалить заявку (и связанные тендеры)."""
    sr = db.query(SearchRequestModel).filter(
        SearchRequestModel.id == request_id,
        SearchRequestModel.user_id == current_user.id,
    ).first()
    if not sr:
        raise HTTPException(404, "Заявка не найдена")
    db.query(Tender).filter(Tender.request_id == sr.id).delete()
    db.delete(sr)
    db.commit()
    return {"ok": True}
