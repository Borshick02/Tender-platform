"""Простая ML-модель: предсказание цены, риск, репутация заказчика.

Использует эвристики + LinearRegression (обучается по мере накопления данных).
"""
import re
import hashlib
import logging
from typing import Optional

import numpy as np
from sklearn.linear_model import LinearRegression

log = logging.getLogger(__name__)

_customer_cache: dict[str, float] = {}

_price_model: Optional[LinearRegression] = None
_price_X: list[list[float]] = []
_price_y: list[float] = []
_MIN_SAMPLES_FOR_MODEL = 30


def _parse_price(price_str: str) -> Optional[float]:
    """Извлекает число из строки цены ('150 000 ₽', '461700.0 RUB' и т.п.)."""
    if not price_str:
        return None
    s = re.sub(r"[^\d\s.,]", "", str(price_str))
    s = s.replace(",", ".").replace(" ", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _encode_law(law_type: Optional[str]) -> float:
    if not law_type:
        return 0.0
    lt = str(law_type)
    if "44" in lt:
        return 1.0
    if "223" in lt:
        return 2.0
    if "615" in lt:
        return 3.0
    return 0.0


def _retrain_price_model():
    """Переобучает LinearRegression, если накопилось достаточно данных."""
    global _price_model
    if len(_price_X) < _MIN_SAMPLES_FOR_MODEL:
        return
    try:
        X = np.array(_price_X)
        y = np.array(_price_y)
        model = LinearRegression()
        model.fit(X, y)
        _price_model = model
        log.info("Price model retrained on %d samples, R²=%.3f", len(y), model.score(X, y))
    except Exception as exc:
        log.warning("Price model training failed: %s", exc)


def add_price_sample(price_numeric: float, law_type: Optional[str] = None,
                     final_price: Optional[float] = None):
    """Добавляет обучающий пример. final_price — реальная финальная цена (если известна)."""
    target = final_price if final_price is not None else price_numeric * 0.93
    _price_X.append([price_numeric, _encode_law(law_type)])
    _price_y.append(target)
    if len(_price_X) % _MIN_SAMPLES_FOR_MODEL == 0:
        _retrain_price_model()


def predict_final_price(price_numeric: Optional[float], source: str,
                        law_type: Optional[str] = None) -> Optional[float]:
    """Предсказывает финальную цену контракта."""
    if price_numeric is None or price_numeric <= 0:
        return None

    if _price_model is not None:
        try:
            features = np.array([[price_numeric, _encode_law(law_type)]])
            pred = float(_price_model.predict(features)[0])
            if pred > 0:
                return round(pred, 2)
        except Exception:
            pass

    if law_type and "44" in str(law_type):
        factor = 0.92
    elif law_type and "223" in str(law_type):
        factor = 0.95
    else:
        factor = 0.93
    return round(price_numeric * factor, 2)


def predict_risk_score(
    price_numeric: Optional[float],
    customer: Optional[str],
    source: str,
    law_type: Optional[str] = None,
) -> float:
    """Оценка риска 'подставного' тендера (0-1)."""
    risk = 0.2
    if price_numeric and price_numeric > 0:
        s = str(int(price_numeric))
        trailing_zeros = len(s) - len(s.rstrip("0"))
        if trailing_zeros >= 4:
            risk += 0.2
        elif trailing_zeros >= 2:
            risk += 0.1
        if price_numeric >= 100_000_000:
            risk += 0.15
        elif price_numeric >= 50_000_000:
            risk += 0.1
        if price_numeric < 10_000:
            risk += 0.05
    if customer:
        cust_upper = customer.upper()
        if any(x in cust_upper for x in ("ИП ", "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ")):
            risk += 0.08
        key = hashlib.md5(customer.encode()).hexdigest()
        if key in _customer_cache:
            rep = _customer_cache[key]
            risk += (1.0 - rep) * 0.2
    if law_type and "615" in str(law_type):
        risk += 0.08
    return min(1.0, round(risk, 2))


def predict_customer_reputation(customer: Optional[str], tender_count: int = 0) -> float:
    """Репутация заказчика 0-1."""
    if not customer:
        return 0.5
    key = customer
    if key in _customer_cache:
        cached = _customer_cache[key]
        if tender_count > 0:
            boost = min(0.15, tender_count * 0.01)
            updated = min(1.0, round(cached + boost, 2))
            _customer_cache[key] = updated
            return updated
        return cached

    base = 0.5
    if tender_count > 20:
        base = 0.85
    elif tender_count > 10:
        base = 0.75
    elif tender_count > 5:
        base = 0.68
    elif tender_count > 3:
        base = 0.62

    cust_upper = customer.upper()
    if any(x in cust_upper for x in ("ГБУ", "ГАУ", "ГУП", "МУП", "ФГУП",
                                      "МИНИСТЕРСТВО", "АДМИНИСТРАЦИЯ", "ДЕПАРТАМЕНТ")):
        base = min(1.0, base + 0.1)
    if any(x in cust_upper for x in ("ИП ", "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ")):
        base = max(0.0, base - 0.05)

    _customer_cache[key] = round(base, 2)
    return _customer_cache[key]


def enrich_tender(tender: dict, customer_tender_count: int = 0) -> dict:
    """Добавляет ML-предсказания к тендеру и накапливает обучающие данные."""
    price_num = tender.get("price_numeric")
    if price_num is None and tender.get("price"):
        price_num = _parse_price(str(tender["price"]))
    source = tender.get("source", "")
    law_type = tender.get("law_type")
    customer = tender.get("customer")

    tender["predicted_price"] = predict_final_price(price_num, source, law_type)
    tender["risk_score"] = predict_risk_score(price_num, customer, source, law_type)
    tender["customer_reputation"] = predict_customer_reputation(customer, customer_tender_count)
    if price_num is not None:
        tender["price_numeric"] = price_num
        add_price_sample(price_num, law_type)
    return tender


def get_model_stats() -> dict:
    """Статистика ML-модели для диагностики."""
    return {
        "price_samples": len(_price_X),
        "model_trained": _price_model is not None,
        "customer_cache_size": len(_customer_cache),
        "min_samples_for_model": _MIN_SAMPLES_FOR_MODEL,
    }
