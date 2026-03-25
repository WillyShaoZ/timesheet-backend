import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import Base, engine, get_db
from routers import webhook
from routers.auth import router as auth_router, hash_password
from models import User

# 启动时自动建表
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Timesheet Backend")

app.add_middleware(
  CORSMiddleware,
  allow_origins=["https://timesheet-frontend-production-10ea.up.railway.app"],
  allow_methods=["*"],
  allow_headers=["*"],
)

app.include_router(webhook.router, prefix="/webhook")
app.include_router(auth_router, prefix="/auth")


def seed_users():
  db = next(get_db())
  try:
    boss_password = os.getenv("BOSS_PASSWORD", "boss2026")
    accountant_password = os.getenv("ACCOUNTANT_PASSWORD", "acc2026")

    if not db.query(User).filter(User.username == "boss").first():
      db.add(User(username="boss", hashed_password=hash_password(boss_password), role="boss"))
      print("[初始化] 创建老板账号 boss")

    if not db.query(User).filter(User.username == "accountant").first():
      db.add(User(username="accountant", hashed_password=hash_password(accountant_password), role="accountant"))
      print("[初始化] 创建会计账号 accountant")

    db.commit()
  finally:
    db.close()

seed_users()


@app.get("/")
def health_check():
  return {"status": "running"}
