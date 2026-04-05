import os
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from database import Base, engine, get_db
from routers import webhook
from routers.auth import router as auth_router, hash_password
from routers.timesheet import router as timesheet_router
from routers.audit import router as audit_router
from models import User, Message

# 启动时自动建表
Base.metadata.create_all(bind=engine)

# 补充新增字段（create_all 不会自动 ALTER 已有表）
def run_migrations():
  migrations = [
    "ALTER TABLE timesheet_entries ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'confirmed'",
    "ALTER TABLE timesheet_entries ADD COLUMN IF NOT EXISTS ai_note TEXT",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS id SERIAL PRIMARY KEY",  # 防止首次建表遗漏
  ]
  with engine.connect() as conn:
    for sql in migrations:
      try:
        conn.execute(text(sql))
      except Exception:
        pass
    conn.commit()

run_migrations()

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
app.include_router(audit_router, prefix="/audit")


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

try:
  seed_users()
except Exception as e:
  print(f"[WARN] seed_users failed (ignorable): {e}")


def cleanup_old_messages():
  db = next(get_db())
  try:
    two_months_ago = datetime.utcnow() - timedelta(days=60)
    deleted = db.query(Message).filter(Message.received_at < two_months_ago).delete()
    db.commit()
    print(f"[定时清理] 删除 {deleted} 条2个月前的旧消息")
  finally:
    db.close()

def cleanup_old_entries():
  from models import TimesheetEntry
  from datetime import date as date_type
  db = next(get_db())
  try:
    one_year_ago = (datetime.utcnow() - timedelta(days=365)).date()
    deleted = db.query(TimesheetEntry).filter(
      TimesheetEntry.date < one_year_ago
    ).delete()
    db.commit()
    print(f"[定时清理] 删除 {deleted} 条2个月前的工时记录")
  finally:
    db.close()

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_messages, "cron", hour=3, minute=0)
scheduler.add_job(cleanup_old_entries, "cron", hour=3, minute=10)
scheduler.start()


@app.get("/")
def health_check():
  return {"status": "running"}
