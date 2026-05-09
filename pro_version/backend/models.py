"""Модели БД."""
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base

MSK = timezone(timedelta(hours=3))

def _now_msk():
    return datetime.now(MSK)

Base = declarative_base()


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(String(128), index=True)
    title = Column(String(1024), nullable=False)
    url = Column(String(2048))
    source = Column(String(64), index=True)
    price_raw = Column(String(128))
    price_numeric = Column(Float)
    customer = Column(String(512))
    organizer = Column(String(512))
    law_type = Column(String(64))
    purchase_type = Column(String(128))
    deadline = Column(String(128))
    status = Column(String(128))
    region = Column(String(256))
    platform = Column(String(256))
    publish_date = Column(String(128))
    extra = Column(Text)
    created_at = Column(DateTime, default=_now_msk)
    search_query = Column(String(256), index=True)

    predicted_price = Column(Float)
    risk_score = Column(Float)
    customer_reputation = Column(Float)

    request_id = Column(Integer, ForeignKey("search_requests.id"), nullable=True, index=True)

    def to_dict(self):
        d = {
            "id": self.id,
            "tender_id": self.tender_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "price": self.price_raw,
            "price_numeric": self.price_numeric,
            "customer": self.customer,
            "organizer": self.organizer,
            "law_type": self.law_type,
            "purchase_type": self.purchase_type,
            "deadline": self.deadline,
            "status": self.status,
            "region": self.region,
            "platform": self.platform,
            "publish_date": self.publish_date,
        }
        if self.predicted_price is not None:
            d["predicted_price"] = round(self.predicted_price, 2)
        if self.risk_score is not None:
            d["risk_score"] = round(self.risk_score, 2)
        if self.customer_reputation is not None:
            d["customer_reputation"] = round(self.customer_reputation, 2)
        return d


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(256), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    name = Column(String(256))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now_msk)

    requests = relationship("SearchRequest", back_populates="user", lazy="dynamic")


class SearchRequest(Base):
    """Заявка на поиск, привязанная к пользователю."""
    __tablename__ = "search_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    query = Column(String(512), nullable=False)
    pages = Column(Integer, default=1)
    sources_json = Column(Text, default="[]")
    date_from = Column(String(32), nullable=True)
    date_to = Column(String(32), nullable=True)
    min_days_left = Column(Integer, nullable=True)

    status = Column(String(32), default="pending", index=True)  # pending / running / completed / error
    progress = Column(Integer, default=0)
    total_results = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    logs_json = Column(Text, default="[]")

    created_at = Column(DateTime, default=_now_msk)
    finished_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="requests")
    tenders = relationship("Tender", backref="search_request", lazy="dynamic")

    @property
    def sources(self):
        try:
            return json.loads(self.sources_json or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    @sources.setter
    def sources(self, val):
        self.sources_json = json.dumps(val or [], ensure_ascii=False)

    @property
    def logs(self):
        try:
            return json.loads(self.logs_json or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    @logs.setter
    def logs(self, val):
        self.logs_json = json.dumps(val or [], ensure_ascii=False)

    def to_dict(self, include_results=False):
        d = {
            "id": self.id,
            "query": self.query,
            "pages": self.pages,
            "sources": self.sources,
            "status": self.status,
            "progress": self.progress,
            "total_results": self.total_results,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if include_results:
            d["results"] = [t.to_dict() for t in self.tenders.all()]
            d["logs"] = self.logs[-100:]
        return d


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False)
    created_at = Column(DateTime, default=_now_msk)
