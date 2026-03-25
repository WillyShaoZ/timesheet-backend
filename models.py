from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.sql import func
from database import Base

class Message(Base):
  __tablename__ = "messages"

  id = Column(Integer, primary_key=True, index=True)
  sender = Column(String, nullable=True)       # 发消息的管工
  sender_id = Column(String, nullable=True)    # 企业微信用户ID
  content = Column(Text, nullable=False)       # 原始消息内容
  raw_payload = Column(Text, nullable=True)    # 完整原始请求体（调试用）
  received_at = Column(DateTime(timezone=True), server_default=func.now())
  processed = Column(Boolean, default=False)   # 是否已被处理
