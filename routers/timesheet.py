import json
from datetime import date
from io import BytesIO
from typing import Optional
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Body, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from database import get_db
from models import TimesheetEntry, AuditLog, User
from routers.auth import get_current_user
import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.utils import get_column_letter

router = APIRouter()


def entry_to_dict(e: TimesheetEntry) -> dict:
  return {
    "date": e.date.isoformat() if e.date else None,
    "address": e.address, "name": e.name,
    "people_count": e.people_count, "hours": e.hours,
    "total_hours": e.total_hours, "verified_hours": e.verified_hours,
    "hourly_rate": e.hourly_rate, "amount": e.amount, "notes": e.notes,
    "status": e.status, "ai_note": e.ai_note,
  }

def entry_row(e: TimesheetEntry) -> dict:
  return {**entry_to_dict(e), "id": e.id, "source_message_id": e.source_message_id}

def write_audit(db, username, action, table, record_id, old_vals=None, new_vals=None):
  db.add(AuditLog(
    username=username, action=action, table_name=table, record_id=record_id,
    old_values=json.dumps(old_vals, ensure_ascii=False) if old_vals else None,
    new_values=json.dumps(new_vals, ensure_ascii=False) if new_vals else None,
  ))


@router.post("/entries/batch")
def create_entries_batch(
  entries: list = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  """本地解析脚本调用此接口批量写入解析结果"""
  created = []
  for e in entries:
    entry = TimesheetEntry(
      date=date.fromisoformat(e["date"]) if e.get("date") else None,
      address=e.get("address", ""),
      name=e.get("name", ""),
      people_count=e.get("people_count", 1),
      hours=e.get("hours"),
      total_hours=e.get("total_hours") or e.get("hours"),
      notes=e.get("notes", ""),
      source_message_id=e.get("source_message_id"),
      status=e.get("status", "confirmed"),
      ai_note=e.get("ai_note"),
    )
    db.add(entry)
    created.append(entry)
  db.flush()
  for entry in created:
    write_audit(db, current_user.username, "CREATE", "timesheet_entries", entry.id, new_vals=entry_to_dict(entry))
  db.commit()
  return {"created": len(created)}


@router.get("/known-names")
def get_known_names(
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  """守护脚本调用，获取已知工人名单"""
  rows = db.query(TimesheetEntry.name).filter(
    TimesheetEntry.name != None,
    TimesheetEntry.status == "confirmed"
  ).distinct().all()
  return {"names": [r.name for r in rows if r.name]}


@router.get("/pending")
def get_pending(
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  items = (
    db.query(TimesheetEntry)
    .filter(TimesheetEntry.status == "pending")
    .order_by(TimesheetEntry.created_at.desc())
    .all()
  )
  return {"total": len(items), "items": [entry_row(e) for e in items]}


@router.post("/entries/{entry_id}/confirm")
def confirm_entry(
  entry_id: int,
  data: dict = Body(default={}),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  entry = db.query(TimesheetEntry).filter(TimesheetEntry.id == entry_id).first()
  if not entry:
    raise HTTPException(status_code=404, detail="记录不存在")
  old = entry_to_dict(entry)
  # 允许确认时顺便修正字段
  allowed = {"name", "address", "date", "hours", "total_hours", "people_count", "notes", "verified_hours"}
  for k, v in data.items():
    if k in allowed:
      setattr(entry, k, v)
  # 确认时若未手动填写核对工时，自动用工时合计回填
  if not entry.verified_hours and entry.total_hours:
    entry.verified_hours = entry.total_hours
  entry.status = "confirmed"
  entry.ai_note = None
  write_audit(db, current_user.username, "CONFIRM", "timesheet_entries", entry_id, old_vals=old, new_vals=entry_to_dict(entry))
  db.commit()
  return {"status": "ok"}


@router.post("/entries/{entry_id}/reject")
def reject_entry(
  entry_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  entry = db.query(TimesheetEntry).filter(TimesheetEntry.id == entry_id).first()
  if not entry:
    raise HTTPException(status_code=404, detail="记录不存在")
  old = entry_to_dict(entry)
  entry.status = "rejected"
  write_audit(db, current_user.username, "REJECT", "timesheet_entries", entry_id, old_vals=old)
  db.commit()
  return {"status": "ok"}


@router.get("/entries")
def get_entries(
  page: int = 1,
  size: int = 500,
  date_from: Optional[str] = Query(None),
  date_to: Optional[str] = Query(None),
  name: Optional[str] = Query(None),
  address: Optional[str] = Query(None),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  q = db.query(TimesheetEntry).filter(TimesheetEntry.status == "confirmed")
  if date_from:
    q = q.filter(TimesheetEntry.date >= date.fromisoformat(date_from))
  if date_to:
    q = q.filter(TimesheetEntry.date <= date.fromisoformat(date_to))
  if name:
    q = q.filter(TimesheetEntry.name.ilike(f"%{name}%"))
  if address:
    q = q.filter(TimesheetEntry.address.ilike(f"%{address}%"))
  total = q.count()
  items = (
    q.order_by(TimesheetEntry.date.asc().nullslast(), TimesheetEntry.id.asc())
    .offset((page - 1) * size)
    .limit(size)
    .all()
  )
  return {
    "total": total,
    "items": [{
      "id": e.id,
      "date": e.date.isoformat() if e.date else None,
      "address": e.address,
      "name": e.name,
      "people_count": e.people_count,
      "hours": e.hours,
      "total_hours": e.total_hours,
      "verified_hours": e.verified_hours,
      "hourly_rate": e.hourly_rate,
      "amount": e.amount,
      "notes": e.notes,
      "source_message_id": e.source_message_id,
    } for e in items]
  }


@router.patch("/entries/{entry_id}")
def update_entry(
  entry_id: int,
  data: dict = Body(...),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  entry = db.query(TimesheetEntry).filter(TimesheetEntry.id == entry_id).first()
  if not entry:
    raise HTTPException(status_code=404, detail="记录不存在")
  old = entry_to_dict(entry)
  allowed = {"verified_hours", "hourly_rate", "amount", "notes", "address", "name", "hours", "total_hours", "people_count", "date"}
  for k, v in data.items():
    if k in allowed:
      if k == "date" and isinstance(v, str) and v:
        v = date.fromisoformat(v)
      setattr(entry, k, v)
  if entry.verified_hours and entry.hourly_rate:
    entry.amount = round(entry.verified_hours * entry.hourly_rate, 2)
  write_audit(db, current_user.username, "UPDATE", "timesheet_entries", entry_id, old_vals=old, new_vals=entry_to_dict(entry))
  db.commit()
  return {"status": "ok"}


@router.delete("/entries/{entry_id}")
def delete_entry(
  entry_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  entry = db.query(TimesheetEntry).filter(TimesheetEntry.id == entry_id).first()
  if not entry:
    raise HTTPException(status_code=404, detail="记录不存在")
  old = entry_to_dict(entry)
  write_audit(db, current_user.username, "DELETE", "timesheet_entries", entry_id, old_vals=old)
  db.delete(entry)
  db.commit()
  return {"status": "ok"}


@router.post("/entries/{entry_id}/restore")
def restore_entry(
  entry_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  """从最近一次修改前的快照回滚"""
  log = (
    db.query(AuditLog)
    .filter(AuditLog.table_name == "timesheet_entries", AuditLog.record_id == entry_id)
    .filter(AuditLog.old_values != None)
    .order_by(desc(AuditLog.created_at))
    .first()
  )
  if not log:
    raise HTTPException(status_code=404, detail="没有可回滚的历史记录")

  old = json.loads(log.old_values)
  entry = db.query(TimesheetEntry).filter(TimesheetEntry.id == entry_id).first()

  if entry:
    # 记录在，更新回旧值
    for k, v in old.items():
      setattr(entry, k, v)
  else:
    # 记录已被删除，重新创建
    entry = TimesheetEntry(id=entry_id, **{k: v for k, v in old.items() if k != "date"})
    if old.get("date"):
      entry.date = date.fromisoformat(old["date"])
    db.add(entry)

  write_audit(db, current_user.username, "RESTORE", "timesheet_entries", entry_id,
              new_vals=old, old_vals={"restored_from_log": log.id})
  db.commit()
  return {"status": "ok", "restored_to": old}


@router.get("/export")
def export_excel(
  date_from: Optional[str] = Query(None),
  date_to: Optional[str] = Query(None),
  name: Optional[str] = Query(None),
  address: Optional[str] = Query(None),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  q = db.query(TimesheetEntry).filter(TimesheetEntry.status == "confirmed")
  if date_from:
    q = q.filter(TimesheetEntry.date >= date.fromisoformat(date_from))
  if date_to:
    q = q.filter(TimesheetEntry.date <= date.fromisoformat(date_to))
  if name:
    q = q.filter(TimesheetEntry.name.ilike(f"%{name}%"))
  if address:
    q = q.filter(TimesheetEntry.address.ilike(f"%{address}%"))
  items = q.order_by(TimesheetEntry.date.asc().nullslast(), TimesheetEntry.id.asc()).all()

  wb = openpyxl.Workbook()
  ws = wb.active
  ws.title = "工时记录"

  # 样式
  header_font = Font(bold=True)
  center = Alignment(horizontal="center", vertical="center")
  thin = Side(style="thin")
  border = Border(left=thin, right=thin, top=thin, bottom=thin)
  header_fill = PatternFill("solid", fgColor="D9E1F2")

  headers = ["序号", "日期", "地址", "姓名", "人数", "工时(h)", "工时合计", "核对工时", "时薪", "金额", "备注"]
  col_widths = [6, 14, 30, 10, 6, 8, 8, 8, 8, 10, 20]

  for col, (h, w) in enumerate(zip(headers, col_widths), 1):
    cell = ws.cell(row=1, column=col, value=h)
    cell.font = header_font
    cell.alignment = center
    cell.border = border
    cell.fill = header_fill
    ws.column_dimensions[get_column_letter(col)].width = w

  ws.row_dimensions[1].height = 20

  for row_idx, e in enumerate(items, 2):
    row_data = [
      row_idx - 1,
      e.date.strftime("%Y-%m-%d") if e.date else "",
      e.address or "",
      e.name or "",
      e.people_count or 1,
      e.hours or "",
      e.total_hours or "",
      e.verified_hours or "",
      e.hourly_rate or "",
      e.amount or "",
      e.notes or "",
    ]
    for col, val in enumerate(row_data, 1):
      cell = ws.cell(row=row_idx, column=col, value=val)
      cell.alignment = center
      cell.border = border

  buf = BytesIO()
  wb.save(buf)
  buf.seek(0)

  filename = f"工时记录_{date.today().isoformat()}.xlsx"
  return StreamingResponse(
    buf,
    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
  )
