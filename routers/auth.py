import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from passlib.context import CryptContext
from database import get_db
from models import User

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "timesheet-jwt-secret-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7天

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain, hashed):
  return pwd_context.verify(plain, hashed)

def hash_password(password):
  return pwd_context.hash(password)

def create_access_token(data: dict):
  to_encode = data.copy()
  expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
  to_encode.update({"exp": expire})
  return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
  credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="无效的登录凭证",
    headers={"WWW-Authenticate": "Bearer"},
  )
  try:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    username: str = payload.get("sub")
    if username is None:
      raise credentials_exception
  except JWTError:
    raise credentials_exception

  user = db.query(User).filter(User.username == username).first()
  if user is None or not user.is_active:
    raise credentials_exception
  return user


@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
  user = db.query(User).filter(User.username == form_data.username).first()
  if not user or not verify_password(form_data.password, user.hashed_password):
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="用户名或密码错误",
    )
  token = create_access_token({"sub": user.username, "role": user.role})
  return {"access_token": token, "token_type": "bearer", "role": user.role}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
  return {"username": current_user.username, "role": current_user.role}
