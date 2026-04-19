"""FastAPI Backend Pro: API тендеров, пользователей, ML + AI-анализ."""
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import engine, get_db
from models import Base
from routers import tenders, users, search
from ml_service import get_model_stats, ai_analyze_tender

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
    backend_dir = Path(__file__).resolve().parent
    project_root = backend_dir.parent

    env_path = os.getenv("FRONTEND_HTML_PATH")
    candidate_paths = [
        Path(env_path).expanduser() if env_path else None,
        project_root / "multi_search_pro.html",
        project_root / "parsers" / "multi_search_pro.html",
    ]

    for pro_path in candidate_paths:
        if pro_path and pro_path.exists():
            return HTMLResponse(pro_path.read_text(encoding="utf-8"))

    checked = "<br>".join(str(path) for path in candidate_paths if path)
    return HTMLResponse(
        "<h1>Тендеры Pro</h1>"
        "<p>Файл multi_search_pro.html не найден.</p>"
        f"<p>Проверены пути:<br>{checked}</p>"
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/ml/stats")
def ml_stats():
    return get_model_stats()


class AIAnalyzeRequest(BaseModel):
    title: str
    price: Optional[str] = None
    customer: Optional[str] = None
    law_type: Optional[str] = None
    purchase_type: Optional[str] = None
    deadline: Optional[str] = None
    region: Optional[str] = None
    source: Optional[str] = None


@app.post("/api/ai/analyze")
def ai_analyze(req: AIAnalyzeRequest):
    """AI-анализ одного тендера — риски, рекомендации, возможности."""
    tender = req.model_dump()
    result = ai_analyze_tender(tender)
    return result
