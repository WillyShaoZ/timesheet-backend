from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from database import get_db
from models import AuditLog, User
from routers.auth import get_current_user

router = APIRouter()


@router.get("/logs")
def get_logs(
  page: int = Query(1, ge=1),
  size: int = Query(50, ge=1, le=200),
  table_name: str = Query(None),
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  q = db.query(AuditLog)
  if table_name:
    q = q.filter(AuditLog.table_name == table_name)
  total = q.count()
  logs = q.order_by(desc(AuditLog.created_at)).offset((page - 1) * size).limit(size).all()
  return {
    "total": total,
    "items": [{
      "id": l.id,
      "username": l.username,
      "action": l.action,
      "table_name": l.table_name,
      "record_id": l.record_id,
      "old_values": l.old_values,
      "new_values": l.new_values,
      "created_at": l.created_at.isoformat() if l.created_at else None,
    } for l in logs]
  }
