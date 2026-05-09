"""API пользователей: регистрация, вход, JWT, профиль."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import User, Favorite
from passlib.context import CryptContext
from jose import jwt, JWTError
import os
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/users", tags=["users"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_EXPIRE = 60 * 60 * 24 * 7  # 7 дней


class UserCreate(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str
    name: Optional[str] = None


def _hash(password: str) -> str:
    return pwd_context.hash(password)


def _verify(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_token(user_id: int, email: str) -> str:
    expire = datetime.utcnow() + timedelta(seconds=ACCESS_EXPIRE)
    return jwt.encode({"sub": str(user_id), "email": email, "exp": expire},
                      SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(authorization: Optional[str] = Header(None),
                     db: Session = Depends(get_db)) -> User:
    """Извлекает текущего пользователя из JWT-токена в заголовке Authorization."""
    if not authorization:
        raise HTTPException(401, "Требуется авторизация")
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(401, "Требуется авторизация")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub", 0))
    except (JWTError, ValueError):
        raise HTTPException(401, "Недействительный токен")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    if not user.is_active:
        raise HTTPException(403, "Аккаунт заблокирован")
    return user


@router.post("/register", response_model=Token)
def register(data: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Пользователь с таким email уже есть")
    user = User(email=data.email, hashed_password=_hash(data.password), name=data.name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return Token(access_token=_create_token(user.id, user.email),
                 user_id=user.id, email=user.email, name=user.name)


@router.post("/login", response_model=Token)
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()
    if not user or not _verify(data.password, user.hashed_password):
        raise HTTPException(401, "Неверный email или пароль")
    if not user.is_active:
        raise HTTPException(403, "Аккаунт заблокирован")
    return Token(access_token=_create_token(user.id, user.email),
                 user_id=user.id, email=user.email, name=user.name)


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "email": current_user.email, "name": current_user.name}


@router.post("/favorites/{tender_id}")
def add_favorite(tender_id: int, current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    fav = Favorite(user_id=current_user.id, tender_id=tender_id)
    db.add(fav)
    db.commit()
    return {"ok": True}


@router.delete("/favorites/{tender_id}")
def remove_favorite(tender_id: int, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    db.query(Favorite).filter(
        Favorite.user_id == current_user.id, Favorite.tender_id == tender_id
    ).delete()
    db.commit()
    return {"ok": True}
