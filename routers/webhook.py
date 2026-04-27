import hashlib
import base64
import json
import os
from Crypto.Cipher import AES
from fastapi import APIRouter, Request, Depends, Query, Header, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from database import get_db
from models import Message, User
from routers.auth import get_current_user

router = APIRouter()

WEBHOOK_API_KEY = os.getenv("WEBHOOK_API_KEY", "timesheet-secret-2026")
WECOM_TOKEN = os.getenv("WECOM_TOKEN", "uX2X7sPo3hLphLfejaocHVA3D0dmaBtw")
WECOM_ENCODING_AES_KEY = os.getenv("WECOM_ENCODING_AES_KEY", "43Ika1HRssOt9Lw5Hvb1yiKmt6Yl4NInigz6vGFmkHJ")


def decrypt_echostr(echostr_encrypted: str) -> str:
    key = base64.b64decode(WECOM_ENCODING_AES_KEY + "=")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    decrypted = cipher.decrypt(base64.b64decode(echostr_encrypted))
    pad = decrypted[-1]
    decrypted = decrypted[:-pad]
    msg_len = int.from_bytes(decrypted[16:20], 'big')
    content = decrypted[20:20 + msg_len]
    return content.decode("utf-8")

@router.get("/wecom", response_class=PlainTextResponse)
async def verify_wecom(
  msg_signature: str = Query(...),
  timestamp: str = Query(...),
  nonce: str = Query(...),
  echostr: str = Query(...)
):
  sign_list = sorted([WECOM_TOKEN, timestamp, nonce, echostr])
  sign_str = "".join(sign_list)
  sha1 = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()
  if sha1 != msg_signature:
    raise HTTPException(status_code=403, detail="签名验证失败")
  return decrypt_echostr(echostr)


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
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
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


@router.patch("/messages/{message_id}/processed")
def mark_message_processed(
  message_id: int,
  db: Session = Depends(get_db),
  current_user: User = Depends(get_current_user)
):
  msg = db.query(Message).filter(Message.id == message_id).first()
  if not msg:
    raise HTTPException(status_code=404, detail="消息不存在")
  msg.processed = True
  db.commit()
  return {"status": "ok"}
