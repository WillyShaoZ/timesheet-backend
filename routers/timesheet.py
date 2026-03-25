from datetime import date
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from database import get_db
from models import TimesheetEntry, User
from routers.auth import get_current_user
import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.utils import get_column_letter

router = APIRouter()


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
      total_hours=e.get("hours"),  # 默认等于工时，可后续核对
      notes=e.get("notes", ""),
      source_message_id=e.get("source_message_id"),
    )
    db.add(entry)
    created.append(entry)
  db.commit()
  return {"created": len(created)}


@router.get("/entries")
def get_entries(
  page: int = 1,
  size: int = 100,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  total = db.query(TimesheetEntry).count()
  items = (
    db.query(TimesheetEntry)
    .order_by(TimesheetEntry.date.asc().nullslast(), TimesheetEntry.id.asc())
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
  allowed = {"verified_hours", "hourly_rate", "amount", "notes", "address", "name", "hours", "total_hours", "people_count"}
  for k, v in data.items():
    if k in allowed:
      setattr(entry, k, v)
  # 自动计算金额
  if entry.verified_hours and entry.hourly_rate:
    entry.amount = round(entry.verified_hours * entry.hourly_rate, 2)
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
  db.delete(entry)
  db.commit()
  return {"status": "ok"}


@router.get("/export")
def export_excel(
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  items = (
    db.query(TimesheetEntry)
    .order_by(TimesheetEntry.date.asc().nullslast(), TimesheetEntry.id.asc())
    .all()
  )

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
    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
  )
