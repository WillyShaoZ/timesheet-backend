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


# 工人 canonical 合并清单：以工时报表图为准。
# (canonical_name, [aliases_to_merge_in_from_DB])
# alias_names 全部合并到 canonical_name；alias 工人记录会被删除并改 timesheet_entries.name
WORKER_MERGES = [
  # 错字 / 同音字
  ("嘉铭", ["嘉明"]),
  ("王昆", ["王坤"]),
  ("汪杨", ["汪扬", "汪洋"]),
  ("啊滨", ["阿彬"]),
  ("宝亮", ["保亮"]),
  ("章智翔", ["张志祥"]),
  ("小施", ["小诗"]),
  # 同音不同字（口字旁 vs 阿字头）
  ("阿豪", ["啊豪"]),
  ("阿山", ["啊山"]),
  ("阿翔", ["啊翔"]),
  ("阿宝", ["啊宝"]),
  # 大小写差异
  ("jacky", ["Jacky"]),
  ("benny", ["Benny"]),
  ("jason", ["Jason"]),
  ("ray", ["Ray"]),
  ("jun", ["Jun"]),
]


def seed_worker_merges():
  """以图为准合并 canonical 重复（错字/大小写不同的同一人）。
  幂等：alias 工人不存在则跳过；已合并的不会重复处理。"""
  from models import Worker, WorkerAlias, TimesheetEntry
  db = next(get_db())
  try:
    pairs_merged = 0
    for canonical_name, alias_names in WORKER_MERGES:
      canonical = db.query(Worker).filter(Worker.canonical_name == canonical_name).first()
      if not canonical:
        # 图里有但 DB 还没这个 canonical：建一个
        canonical = Worker(
          canonical_name=canonical_name,
          status="confirmed",
          confirmed_by="merge_seed",
          confirmed_at=datetime.utcnow(),
          notes="合并时新建",
        )
        db.add(canonical)
        db.flush()

      for alias_name in alias_names:
        old = db.query(Worker).filter(Worker.canonical_name == alias_name).first()
        if not old or old.id == canonical.id:
          continue  # 不存在或已是同一条
        # 1. 改 timesheet_entries.name
        n_entries = (
          db.query(TimesheetEntry)
          .filter(TimesheetEntry.name == alias_name)
          .update({"name": canonical_name}, synchronize_session=False)
        )
        # 2. 把 old 的 aliases 转给 canonical
        db.query(WorkerAlias).filter(WorkerAlias.canonical_id == old.id).update(
          {"canonical_id": canonical.id}, synchronize_session=False
        )
        # 3. 把 alias_name 本身加为 canonical 的 alias（已存在则跳过）
        existing = (
          db.query(WorkerAlias)
          .filter(WorkerAlias.alias == alias_name, WorkerAlias.canonical_id == canonical.id)
          .first()
        )
        if not existing:
          db.add(WorkerAlias(
            alias=alias_name,
            canonical_id=canonical.id,
            status="confirmed",
            occurrence_count=1,
            last_seen_at=datetime.utcnow(),
          ))
        # 4. 把 old 的有用字段移到 canonical（仅在 canonical 没设置时）
        if canonical.default_hourly_rate is None and old.default_hourly_rate is not None:
          canonical.default_hourly_rate = old.default_hourly_rate
        if (canonical.employment_type or "casual") == "casual" and old.employment_type == "formal":
          canonical.employment_type = "formal"
        if old.notes and old.notes not in (canonical.notes or ""):
          canonical.notes = ((canonical.notes + " | ") if canonical.notes else "") + old.notes
        # 5. 删除 old worker
        db.delete(old)
        db.flush()
        pairs_merged += 1
        print(f"[合并] '{alias_name}' → '{canonical_name}' (改 {n_entries} 条 entries)")
    db.commit()
    if pairs_merged:
      print(f"[初始化] 工人合并完成：共 {pairs_merged} 对合并")
  finally:
    db.close()


try:
  seed_worker_merges()
except Exception as e:
  print(f"[WARN] seed_worker_merges failed (ignorable): {e}")


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


# 默认时薪（从 2026.02.23-2026.03.08 工时报表图扫出来）
# 老板可在"员工"页随时改；这里只在 default_hourly_rate 为空时灌入，避免覆盖已设置值
WORKER_DEFAULT_RATES = {
  # 23 名正式员工
  "小林": 45.0, "小汤": 45.0, "小黄": 45.0, "小录": 45.0, "小孙": 45.0,
  "小陈": 45.0, "小全": 45.0, "小于": 45.0, "小薛": 45.0, "殿军": 45.0,
  "小宝": 45.0,  # 报表里写 45(55)，主时薪 45
  "阿俊": 45.0, "嘉铭": 45.0, "老夏": 50.0, "张新宇": 45.0,
  "王昆": 45.0,  # 报表里写 45(55)，主时薪 45
  "汪杨": 45.0, "dave": 45.0, "Eric": 45.0, "jacky": 45.0,
  "nate": 65.0, "tony": 45.0, "ray": 45.0,
  # 其他工人
  "benny": 45.0, "jason": 45.0, "jun": 65.0, "Simon": 40.0,
  "啊滨": 65.0, "阿宝": 45.0, "阿昌": 46.0, "阿豪": 47.0,
  "阿山": 70.0, "阿翔": 48.0, "阿叶": 40.0, "安仔": 65.0,
  "宝亮": 47.0, "大伟": 41.0, "国杰": 65.0, "华峰": 47.0,
  "凯文": 50.0, "文华": 43.0, "老赵": 43.0, "小谷": 44.0,
  "小海": 45.0, "小马": 40.0, "小胖": 40.0, "小吴": 50.0,
  "小杨": 45.0, "于嘉伟": 46.0, "小钟": 43.0, "小施": 49.0,
  "小张": 45.0, "黄勤坤": 45.0, "张军": 44.0, "章智翔": 45.0,
}


def seed_default_hourly_rates():
  """把表格里的时薪灌进 workers.default_hourly_rate。
  幂等：只填空值，不覆盖已设置的（避免每次部署把老板手工改的值冲掉）。
  匹配不上的姓名记录到日志。"""
  from models import Worker
  db = next(get_db())
  try:
    filled = 0
    skipped_already_set = 0
    unmatched = []
    for name, rate in WORKER_DEFAULT_RATES.items():
      w = db.query(Worker).filter(Worker.canonical_name == name).first()
      if not w:
        unmatched.append(name)
        continue
      if w.default_hourly_rate is None:
        w.default_hourly_rate = rate
        filled += 1
      else:
        skipped_already_set += 1
    db.commit()
    if filled:
      print(f"[初始化] 灌入 {filled} 名工人的默认时薪")
    if skipped_already_set:
      print(f"[初始化] {skipped_already_set} 名工人已有时薪，跳过（不覆盖）")
    if unmatched:
      print(f"[初始化] 未在花名册找到这些工人的时薪（请手工处理）：{unmatched}")
  finally:
    db.close()


try:
  seed_default_hourly_rates()
except Exception as e:
  print(f"[WARN] seed_default_hourly_rates failed (ignorable): {e}")


def seed_backfill_entry_rates():
  """把历史 timesheet_entries 中 hourly_rate 为空的，从 worker.default_hourly_rate 补上；
  amount 为空且能算的也一并补上。幂等：只补 NULL，不覆盖已有值。"""
  from models import TimesheetEntry, Worker
  db = next(get_db())
  try:
    workers = db.query(Worker).filter(Worker.default_hourly_rate != None).all()
    rate_by_name = {w.canonical_name: w.default_hourly_rate for w in workers}
    if not rate_by_name:
      return

    # 只查 hourly_rate 为空且 name 在花名册里的 entries
    entries = (
      db.query(TimesheetEntry)
      .filter(TimesheetEntry.hourly_rate.is_(None))
      .filter(TimesheetEntry.name.in_(list(rate_by_name.keys())))
      .all()
    )
    rate_filled = 0
    amount_filled = 0
    for e in entries:
      rate = rate_by_name.get(e.name)
      if rate is None:
        continue
      e.hourly_rate = rate
      rate_filled += 1
      if e.amount is None:
        hrs = e.verified_hours if e.verified_hours is not None else (
          e.total_hours if e.total_hours is not None else e.hours
        )
        if hrs is not None:
          e.amount = round(hrs * rate, 2)
          amount_filled += 1
    db.commit()
    if rate_filled:
      print(f"[初始化] 回填 {rate_filled} 条 entries 的时薪 + {amount_filled} 条金额")
  finally:
    db.close()


try:
  seed_backfill_entry_rates()
except Exception as e:
  print(f"[WARN] seed_backfill_entry_rates failed (ignorable): {e}")


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
