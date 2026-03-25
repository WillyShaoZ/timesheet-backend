import os
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from database import Base, engine, get_db
from routers import webhook
from routers.auth import router as auth_router, hash_password
from routers.timesheet import router as timesheet_router
from models import User, Message

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
app.include_router(timesheet_router, prefix="/timesheet")


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


def cleanup_old_messages():
  db = next(get_db())
  try:
    two_months_ago = datetime.utcnow() - timedelta(days=60)
    deleted = db.query(Message).filter(Message.received_at < two_months_ago).delete()
    db.commit()
    print(f"[定时清理] 删除 {deleted} 条2个月前的旧消息")
  finally:
    db.close()

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_messages, "cron", hour=3, minute=0)
scheduler.start()


@app.get("/")
def health_check():
  return {"status": "running"}
