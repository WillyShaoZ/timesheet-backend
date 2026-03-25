import json
import os
from fastapi import APIRouter, Request, Depends, Query, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from database import get_db
from models import Message

router = APIRouter()

WEBHOOK_API_KEY = os.getenv("WEBHOOK_API_KEY", "timesheet-secret-2026")

@router.post("/wecom")
async def receive_wecom_message(
  request: Request,
  db: Session = Depends(get_db),
  x_api_key: str = Header(None)
):
  if x_api_key != WEBHOOK_API_KEY:
    raise HTTPException(status_code=401, detail="Unauthorized")
  raw_body = await request.body()
  raw_str = raw_body.decode("utf-8")

  sender_id = request.headers.get("x-wecom-userid") or request.headers.get("x-userid")
  sender = None
  content = ""

  try:
    if raw_str:
      payload = json.loads(raw_str)
      content = (
        payload.get("Content")
        or payload.get("content")
        or payload.get("text", {}).get("content")
        or payload.get("message")
        or raw_str
      )
      sender = (
        payload.get("Sender")
        or payload.get("sender")
        or sender_id
      )
  except Exception:
    content = raw_str

  msg = Message(
    sender=sender,
    sender_id=sender_id,
    content=content,
    raw_payload=raw_str,
  )
  db.add(msg)
  db.commit()
  db.refresh(msg)

  print(f"[收到消息] id={msg.id} sender={sender} content={content[:50]}")

  return {"status": "ok", "id": msg.id}


@router.get("/messages")
def get_messages(
  page: int = Query(1, ge=1),
  size: int = Query(20, ge=1, le=100),
  db: Session = Depends(get_db)
):
  total = db.query(Message).count()
  messages = (
    db.query(Message)
    .order_by(desc(Message.received_at))
    .offset((page - 1) * size)
    .limit(size)
    .all()
  )
  return {
    "total": total,
    "page": page,
    "size": size,
    "items": [
      {
        "id": m.id,
        "sender": m.sender,
        "content": m.content,
        "raw_payload": m.raw_payload,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "processed": m.processed,
      }
      for m in messages
    ]
  }
