"""AI 解析知识库 + Prompt 版本化 API

供本地解析脚本（任意电脑上的 Claude / Python 脚本）调用：
  - 拉取当前 prompt + 花名册 + 别名 + 地址簿
  - 解析时观察到的新名字/可疑别名/新地址回写为 pending
  - 提议新 prompt 版本（待老板审核）

供前端"知识库管理"页调用：
  - 列出 / 确认 / 驳回 各类 pending 项
  - 列出 prompt 版本历史 / 激活某版本（回滚或上线新版）
"""

from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Body, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from database import get_db
from models import Worker, WorkerAlias, Site, PromptVersion, Message, User
from routers.auth import get_current_user

router = APIRouter()

# 用户主动确认 ≥ 该次数 → alias 自动归并（status: pending → auto_resolved）
ALIAS_AUTO_RESOLVE_THRESHOLD = 2


# ==================== Prompt ====================

@router.get("/prompt/active")
def get_active_prompt(db: Session = Depends(get_db)):
  """
  本地解析脚本的主入口：拿到当前生效 prompt + 完整知识库快照。
  无需鉴权（解析脚本可能跑在多台电脑上，简化接入；如需鉴权后续加 API key）。
  """
  pv = db.query(PromptVersion).filter(PromptVersion.is_active == True).first()

  workers = db.query(Worker).filter(Worker.status == "confirmed").all()
  aliases = (
    db.query(WorkerAlias)
    .filter(WorkerAlias.status.in_(["confirmed", "auto_resolved"]))
    .all()
  )
  sites = db.query(Site).filter(Site.status == "confirmed").all()

  # 组装：每个 canonical worker 带它已确认的 alias 列表
  alias_map: dict = {}
  for a in aliases:
    alias_map.setdefault(a.canonical_id, []).append(a.alias)

  return {
    "prompt_version": pv.version if pv else None,
    "system_prompt": pv.content if pv else None,
    "workers": [
      {
        "canonical_name": w.canonical_name,
        "aliases": alias_map.get(w.id, []),
        "notes": w.notes,
      }
      for w in workers
    ],
    "sites": [{"address": s.address, "notes": s.notes} for s in sites],
  }


@router.get("/prompt/versions")
def list_prompt_versions(
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  rows = db.query(PromptVersion).order_by(desc(PromptVersion.version)).all()
  return {
    "items": [
      {
        "id": r.id,
        "version": r.version,
        "is_active": r.is_active,
        "status": r.status,
        "created_by": r.created_by,
        "change_note": r.change_note,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "content": r.content,
      }
      for r in rows
    ]
  }


@router.post("/prompt/propose")
def propose_prompt(
  data: dict = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """Claude/本地脚本/老板提议新 prompt，写入 status=proposed 待审核激活"""
  content = data.get("content")
  change_note = data.get("change_note", "")
  created_by = data.get("created_by") or current_user.username
  if not content:
    raise HTTPException(status_code=400, detail="content 必填")

  next_version = (db.query(PromptVersion).count() or 0) + 1
  pv = PromptVersion(
    version=next_version,
    content=content,
    is_active=False,
    status="proposed",
    created_by=created_by,
    change_note=change_note,
  )
  db.add(pv)
  db.commit()
  db.refresh(pv)
  return {"id": pv.id, "version": pv.version, "status": pv.status}


@router.post("/prompt/{version_id}/activate")
def activate_prompt(
  version_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """老板审核通过，把某版本设为生效（同时把原 active 设为 superseded）"""
  pv = db.query(PromptVersion).filter(PromptVersion.id == version_id).first()
  if not pv:
    raise HTTPException(status_code=404, detail="版本不存在")
  if pv.status == "rejected":
    raise HTTPException(status_code=400, detail="该版本已驳回，不能激活")

  # 把当前 active 的版本降级
  active = db.query(PromptVersion).filter(PromptVersion.is_active == True).all()
  for a in active:
    a.is_active = False
    a.status = "superseded"

  pv.is_active = True
  pv.status = "active"
  pv.reviewed_by = current_user.username
  pv.reviewed_at = datetime.utcnow()
  db.commit()
  return {"status": "ok", "active_version": pv.version}


@router.post("/prompt/{version_id}/reject")
def reject_prompt(
  version_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  pv = db.query(PromptVersion).filter(PromptVersion.id == version_id).first()
  if not pv:
    raise HTTPException(status_code=404, detail="版本不存在")
  if pv.is_active:
    raise HTTPException(status_code=400, detail="不能驳回当前生效版本")
  pv.status = "rejected"
  pv.reviewed_by = current_user.username
  pv.reviewed_at = datetime.utcnow()
  db.commit()
  return {"status": "ok"}


# ==================== 工人花名册 ====================

@router.get("/knowledge/workers")
def list_workers(
  status: Optional[str] = Query(None, description="pending / confirmed，缺省返回全部"),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  q = db.query(Worker)
  if status:
    q = q.filter(Worker.status == status)
  workers = q.order_by(desc(Worker.created_at)).all()

  # 拉每个 worker 的 alias 子集
  worker_ids = [w.id for w in workers]
  aliases = (
    db.query(WorkerAlias).filter(WorkerAlias.canonical_id.in_(worker_ids)).all()
    if worker_ids else []
  )
  alias_by_worker: dict = {}
  for a in aliases:
    alias_by_worker.setdefault(a.canonical_id, []).append({
      "id": a.id,
      "alias": a.alias,
      "status": a.status,
      "occurrence_count": a.occurrence_count,
    })

  return {
    "items": [
      {
        "id": w.id,
        "canonical_name": w.canonical_name,
        "status": w.status,
        "first_seen_message_id": w.first_seen_message_id,
        "confirmed_by": w.confirmed_by,
        "confirmed_at": w.confirmed_at.isoformat() if w.confirmed_at else None,
        "notes": w.notes,
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "aliases": alias_by_worker.get(w.id, []),
        # 员工档案字段
        "employment_type": w.employment_type or "casual",
        "is_active": bool(w.is_active) if w.is_active is not None else True,
        "default_hourly_rate": w.default_hourly_rate,
        "abn": w.abn,
      }
      for w in workers
    ]
  }


@router.patch("/knowledge/workers/{worker_id}/employment")
def update_worker_employment(
  worker_id: int,
  data: dict = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """更新员工档案字段：employment_type / is_active / default_hourly_rate / abn / notes。
  仅传入需要修改的字段；不传的字段保持不变。

  特殊副作用：当 default_hourly_rate 从空变成有值（或继续保持有值）时，
  自动回填该工人**所有 hourly_rate 为 NULL 的 timesheet_entries**（包括 amount）。
  已经有 hourly_rate 的 entry 不动（保留历史快照）。"""
  from models import TimesheetEntry
  w = db.query(Worker).filter(Worker.id == worker_id).first()
  if not w:
    raise HTTPException(status_code=404, detail="工人不存在")

  if "employment_type" in data:
    val = data["employment_type"]
    if val not in ("formal", "casual", "temp"):
      raise HTTPException(status_code=400, detail="employment_type 须为 formal/casual/temp")
    w.employment_type = val
  if "is_active" in data:
    w.is_active = bool(data["is_active"])
  if "default_hourly_rate" in data:
    rate = data["default_hourly_rate"]
    if rate is not None and rate != "":
      try:
        w.default_hourly_rate = float(rate)
      except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="default_hourly_rate 必须为数字")
    else:
      w.default_hourly_rate = None
  if "abn" in data:
    w.abn = (data.get("abn") or "").strip() or None
  if "notes" in data:
    w.notes = data["notes"]

  # 回填历史 entries（只动 hourly_rate 为空的）
  backfilled = 0
  if w.default_hourly_rate is not None:
    null_entries = (
      db.query(TimesheetEntry)
      .filter(TimesheetEntry.name == w.canonical_name)
      .filter(TimesheetEntry.hourly_rate.is_(None))
      .all()
    )
    rate_val = float(w.default_hourly_rate)
    for e in null_entries:
      e.hourly_rate = rate_val
      if e.amount is None:
        hrs = e.verified_hours if e.verified_hours is not None else (
          e.total_hours if e.total_hours is not None else e.hours
        )
        if hrs is not None:
          e.amount = round(hrs * rate_val, 2)
      backfilled += 1

  db.commit()
  return {
    "status": "ok",
    "id": w.id,
    "canonical_name": w.canonical_name,
    "employment_type": w.employment_type,
    "is_active": w.is_active,
    "default_hourly_rate": w.default_hourly_rate,
    "abn": w.abn,
    "backfilled_entries": backfilled,
  }


@router.post("/knowledge/workers")
def create_worker(
  data: dict = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """老板手动新增工人（直接 confirmed），或 LLM 观察到新名字进 pending"""
  name = (data.get("canonical_name") or "").strip()
  if not name:
    raise HTTPException(status_code=400, detail="canonical_name 必填")

  existing = db.query(Worker).filter(Worker.canonical_name == name).first()
  if existing:
    return {"id": existing.id, "status": existing.status, "duplicate": True}

  status = data.get("status") or "pending"
  w = Worker(
    canonical_name=name,
    status=status,
    first_seen_message_id=data.get("first_seen_message_id"),
    notes=data.get("notes"),
    confirmed_by=current_user.username if status == "confirmed" else None,
    confirmed_at=datetime.utcnow() if status == "confirmed" else None,
  )
  db.add(w)
  db.commit()
  db.refresh(w)
  return {"id": w.id, "status": w.status, "duplicate": False}


@router.post("/knowledge/workers/{worker_id}/confirm")
def confirm_worker(
  worker_id: int,
  data: dict = Body(default={}),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """老板把 pending 工人确认入花名册（可同时改名/加备注）"""
  w = db.query(Worker).filter(Worker.id == worker_id).first()
  if not w:
    raise HTTPException(status_code=404, detail="工人不存在")
  new_name = (data.get("canonical_name") or "").strip()
  if new_name and new_name != w.canonical_name:
    if db.query(Worker).filter(Worker.canonical_name == new_name).first():
      raise HTTPException(status_code=409, detail="该姓名已存在")
    w.canonical_name = new_name
  if "notes" in data:
    w.notes = data["notes"]
  w.status = "confirmed"
  w.confirmed_by = current_user.username
  w.confirmed_at = datetime.utcnow()
  db.commit()
  return {"status": "ok"}


@router.post("/knowledge/workers/{worker_id}/reject")
def reject_worker(
  worker_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """驳回 pending 工人（直接删除，连带 alias）"""
  w = db.query(Worker).filter(Worker.id == worker_id).first()
  if not w:
    raise HTTPException(status_code=404, detail="工人不存在")
  if w.status == "confirmed":
    raise HTTPException(status_code=400, detail="已确认工人不能驳回，需走删除")
  db.query(WorkerAlias).filter(WorkerAlias.canonical_id == w.id).delete()
  db.delete(w)
  db.commit()
  return {"status": "ok"}


# ==================== 别名映射 ====================

@router.post("/knowledge/aliases")
def create_alias(
  data: dict = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """老板手动添加别名映射（如 '老张'→'张伟'）。
  默认 status='confirmed'（手动加视为已确认）；occurrence_count 设为阈值以上以表示已生效。"""
  alias = (data.get("alias") or "").strip()
  canonical_id = data.get("canonical_id")
  canonical_name = (data.get("canonical_name") or "").strip()
  if not alias:
    raise HTTPException(status_code=400, detail="alias 必填")

  canon = None
  if canonical_id:
    canon = db.query(Worker).filter(Worker.id == canonical_id).first()
  elif canonical_name:
    canon = db.query(Worker).filter(Worker.canonical_name == canonical_name).first()
  if not canon:
    raise HTTPException(status_code=404, detail="找不到对应的 canonical 工人")
  if canon.canonical_name == alias:
    raise HTTPException(status_code=400, detail="别名不能和 canonical 同名")

  existing = (
    db.query(WorkerAlias)
    .filter(WorkerAlias.alias == alias, WorkerAlias.canonical_id == canon.id)
    .first()
  )
  if existing:
    return {"id": existing.id, "status": existing.status, "duplicate": True}

  status = data.get("status") or "confirmed"
  a = WorkerAlias(
    alias=alias,
    canonical_id=canon.id,
    status=status,
    occurrence_count=ALIAS_AUTO_RESOLVE_THRESHOLD if status in ("confirmed", "auto_resolved") else 0,
    last_seen_at=datetime.utcnow(),
  )
  db.add(a)
  db.commit()
  db.refresh(a)
  return {"id": a.id, "status": a.status, "duplicate": False}


@router.get("/knowledge/aliases")
def list_aliases(
  status: Optional[str] = Query(None),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  q = db.query(WorkerAlias)
  if status:
    q = q.filter(WorkerAlias.status == status)
  rows = q.order_by(desc(WorkerAlias.created_at)).all()

  # 一次拿完 canonical_name 减少查询
  ids = list({r.canonical_id for r in rows})
  workers = (
    {w.id: w.canonical_name for w in db.query(Worker).filter(Worker.id.in_(ids)).all()}
    if ids else {}
  )

  return {
    "items": [
      {
        "id": r.id,
        "alias": r.alias,
        "canonical_id": r.canonical_id,
        "canonical_name": workers.get(r.canonical_id),
        "status": r.status,
        "occurrence_count": r.occurrence_count,
        "first_seen_message_id": r.first_seen_message_id,
        "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
      }
      for r in rows
    ]
  }


@router.post("/knowledge/aliases/{alias_id}/confirm")
def confirm_alias(
  alias_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  """
  老板确认"该别名确实指向该 canonical"。
  occurrence_count + 1，达阈值则 status=auto_resolved。
  """
  a = db.query(WorkerAlias).filter(WorkerAlias.id == alias_id).first()
  if not a:
    raise HTTPException(status_code=404, detail="别名不存在")

  a.occurrence_count = (a.occurrence_count or 0) + 1
  a.last_seen_at = datetime.utcnow()
  if a.status == "pending" and a.occurrence_count >= ALIAS_AUTO_RESOLVE_THRESHOLD:
    a.status = "auto_resolved"
  db.commit()
  return {
    "status": "ok",
    "alias_status": a.status,
    "occurrence_count": a.occurrence_count,
    "auto_resolved": a.status == "auto_resolved",
  }


@router.post("/knowledge/aliases/{alias_id}/reject")
def reject_alias(
  alias_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  a = db.query(WorkerAlias).filter(WorkerAlias.id == alias_id).first()
  if not a:
    raise HTTPException(status_code=404, detail="别名不存在")
  db.delete(a)
  db.commit()
  return {"status": "ok"}


# ==================== 地址簿 ====================

@router.get("/knowledge/sites")
def list_sites(
  status: Optional[str] = Query(None),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  q = db.query(Site)
  if status:
    q = q.filter(Site.status == status)
  rows = q.order_by(desc(Site.created_at)).all()
  return {
    "items": [
      {
        "id": s.id,
        "address": s.address,
        "status": s.status,
        "first_seen_message_id": s.first_seen_message_id,
        "confirmed_by": s.confirmed_by,
        "confirmed_at": s.confirmed_at.isoformat() if s.confirmed_at else None,
        "notes": s.notes,
        "created_at": s.created_at.isoformat() if s.created_at else None,
      }
      for s in rows
    ]
  }


@router.post("/knowledge/sites")
def create_site(
  data: dict = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  addr = (data.get("address") or "").strip()
  if not addr:
    raise HTTPException(status_code=400, detail="address 必填")
  existing = db.query(Site).filter(Site.address == addr).first()
  if existing:
    return {"id": existing.id, "status": existing.status, "duplicate": True}

  status = data.get("status") or "pending"
  s = Site(
    address=addr,
    status=status,
    first_seen_message_id=data.get("first_seen_message_id"),
    notes=data.get("notes"),
    confirmed_by=current_user.username if status == "confirmed" else None,
    confirmed_at=datetime.utcnow() if status == "confirmed" else None,
  )
  db.add(s)
  db.commit()
  db.refresh(s)
  return {"id": s.id, "status": s.status, "duplicate": False}


@router.post("/knowledge/sites/{site_id}/confirm")
def confirm_site(
  site_id: int,
  data: dict = Body(default={}),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  s = db.query(Site).filter(Site.id == site_id).first()
  if not s:
    raise HTTPException(status_code=404, detail="地址不存在")
  new_addr = (data.get("address") or "").strip()
  if new_addr and new_addr != s.address:
    if db.query(Site).filter(Site.address == new_addr).first():
      raise HTTPException(status_code=409, detail="该地址已存在")
    s.address = new_addr
  if "notes" in data:
    s.notes = data["notes"]
  s.status = "confirmed"
  s.confirmed_by = current_user.username
  s.confirmed_at = datetime.utcnow()
  db.commit()
  return {"status": "ok"}


@router.post("/knowledge/sites/{site_id}/reject")
def reject_site(
  site_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user),
):
  s = db.query(Site).filter(Site.id == site_id).first()
  if not s:
    raise HTTPException(status_code=404, detail="地址不存在")
  if s.status == "confirmed":
    raise HTTPException(status_code=400, detail="已确认地址不能驳回")
  db.delete(s)
  db.commit()
  return {"status": "ok"}


# ==================== 观察回写（本地解析脚本主用） ====================

@router.post("/observations")
def submit_observations(
  data: dict = Body(...),
  db: Session = Depends(get_db),
):
  """
  本地解析脚本/Claude 解析完一批消息后回写观察结果。无需鉴权（同 /prompt/active）。

  Body 示例：
  {
    "new_workers": [
      {"canonical_name": "李建国", "first_seen_message_id": 42}
    ],
    "suspected_aliases": [
      {"alias": "小五", "canonical_name": "小吴", "first_seen_message_id": 42}
    ],
    "new_sites": [
      {"address": "204 Canadian Bay Rd", "first_seen_message_id": 42}
    ]
  }

  所有写入均为 status=pending，等老板在网页上审核确认。
  """
  result = {"workers_added": 0, "aliases_added": 0, "sites_added": 0}

  for w in data.get("new_workers", []) or []:
    name = (w.get("canonical_name") or "").strip()
    if not name:
      continue
    if db.query(Worker).filter(Worker.canonical_name == name).first():
      continue
    db.add(Worker(
      canonical_name=name,
      status="pending",
      first_seen_message_id=w.get("first_seen_message_id"),
    ))
    result["workers_added"] += 1

  db.flush()

  for al in data.get("suspected_aliases", []) or []:
    alias = (al.get("alias") or "").strip()
    canon_name = (al.get("canonical_name") or "").strip()
    if not (alias and canon_name):
      continue
    canon = db.query(Worker).filter(Worker.canonical_name == canon_name).first()
    if not canon:
      # canonical 还不存在则跳过；本地脚本应当先把 canonical 加进 new_workers
      continue
    existing = (
      db.query(WorkerAlias)
      .filter(WorkerAlias.alias == alias, WorkerAlias.canonical_id == canon.id)
      .first()
    )
    if existing:
      existing.last_seen_at = datetime.utcnow()
      continue
    db.add(WorkerAlias(
      alias=alias,
      canonical_id=canon.id,
      status="pending",
      occurrence_count=0,
      first_seen_message_id=al.get("first_seen_message_id"),
      last_seen_at=datetime.utcnow(),
    ))
    result["aliases_added"] += 1

  for s in data.get("new_sites", []) or []:
    addr = (s.get("address") or "").strip()
    if not addr:
      continue
    if db.query(Site).filter(Site.address == addr).first():
      continue
    db.add(Site(
      address=addr,
      status="pending",
      first_seen_message_id=s.get("first_seen_message_id"),
    ))
    result["sites_added"] += 1

  db.commit()
  return {"status": "ok", **result}
