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
from routers.parsing import router as parsing_router
from models import User, Message

# 启动时自动建表
Base.metadata.create_all(bind=engine)

# 补充新增字段（create_all 不会自动 ALTER 已有表）
def run_migrations():
  migrations = [
    "ALTER TABLE timesheet_entries ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'confirmed'",
    "ALTER TABLE timesheet_entries ADD COLUMN IF NOT EXISTS ai_note TEXT",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS id SERIAL PRIMARY KEY",  # 防止首次建表遗漏
    # 员工档案字段（2026-05-07）
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS employment_type VARCHAR DEFAULT 'casual'",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS default_hourly_rate FLOAT",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS abn VARCHAR",
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
app.include_router(parsing_router, prefix="/parsing")


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


def seed_knowledge_base():
  """首次启动自动冷启动知识库：灌历史 entries 进 workers/sites + 写入 v1 prompt。
  幂等：prompt_versions 表非空则跳过，避免覆盖老板已有的 prompt 修改。"""
  from models import PromptVersion
  from scripts.init_knowledge_base import (
    seed_workers_from_entries,
    seed_sites_from_entries,
    seed_prompt_v1,
  )
  db = next(get_db())
  try:
    if db.query(PromptVersion).count() > 0:
      return
    n_w = seed_workers_from_entries(db)
    n_s = seed_sites_from_entries(db)
    seed_prompt_v1(db)
    print(f"[初始化] 知识库冷启动完成：{n_w} 工人 / {n_s} 地址 / v1 prompt")
  finally:
    db.close()

try:
  seed_knowledge_base()
except Exception as e:
  print(f"[WARN] seed_knowledge_base failed (ignorable): {e}")


# 23 个老板手工指定的"正式员工"名单（来自 2026.02.23-2026.03.08 工时报表右侧分账栏）
# 名字与数据库 canonical_name 精确匹配；匹配不上的进 unmatched 清单，老板后续手动处理
FORMAL_EMPLOYEE_NAMES = [
  "小林", "小汤", "小黄", "小录", "小孙", "小陈", "小全", "小于", "小薛", "殿军",
  "小宝", "阿俊", "嘉铭", "老夏", "张新宇", "王昆", "汪杨", "dave", "Eric",
  "jacky", "nate", "tony", "ray",
]


def seed_formal_employees():
  """把上面 23 个名字标 employment_type='formal'。
  幂等：每次启动都跑，已是 formal 的跳过；匹配不上的打印警告。"""
  from models import Worker
  db = next(get_db())
  try:
    matched = 0
    unmatched = []
    for name in FORMAL_EMPLOYEE_NAMES:
      w = db.query(Worker).filter(Worker.canonical_name == name).first()
      if not w:
        unmatched.append(name)
        continue
      if w.employment_type != "formal":
        w.employment_type = "formal"
        matched += 1
    db.commit()
    if matched:
      print(f"[初始化] 标记 {matched} 个工人为正式员工")
    if unmatched:
      print(f"[初始化] 未在花名册找到这些正式员工（请手工处理）：{unmatched}")
  finally:
    db.close()


try:
  seed_formal_employees()
except Exception as e:
  print(f"[WARN] seed_formal_employees failed (ignorable): {e}")


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
