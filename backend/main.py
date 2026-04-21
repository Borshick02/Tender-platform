"""FastAPI Backend Pro: API тендеров, пользователей, ML."""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from database import engine, get_db
from models import Base
from routers import tenders, users, search
from ml_service import get_model_stats

app = FastAPI(title="Тендеры Pro API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tenders.router)
app.include_router(users.router)
app.include_router(search.router)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


@app.get("/", response_class=HTMLResponse)
def index():
    """Главная — Pro-страница поиска."""
    pro_path = Path(__file__).parent.parent / "multi_search_pro.html"
    if pro_path.exists():
        return HTMLResponse(pro_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Тендеры Pro</h1><p>Файл multi_search_pro.html не найден.</p>")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/ml/stats")
def ml_stats():
    """Статистика ML-модели."""
    return get_model_stats()
