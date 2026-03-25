from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, Float, Text, ForeignKey
from sqlalchemy.sql import func
from database import Base

class User(Base):
  __tablename__ = "users"

  id = Column(Integer, primary_key=True, index=True)
  username = Column(String, unique=True, nullable=False, index=True)
  hashed_password = Column(String, nullable=False)
  role = Column(String, nullable=False, default="viewer")  # boss / accountant
  is_active = Column(Boolean, default=True)

class TimesheetEntry(Base):
  __tablename__ = "timesheet_entries"

  id = Column(Integer, primary_key=True, index=True)
  date = Column(Date, nullable=True)                      # 日期
  address = Column(String, nullable=True)                 # 地址
  name = Column(String, nullable=True)                    # 姓名
  people_count = Column(Integer, default=1)               # 人数
  hours = Column(Float, nullable=True)                    # 工时(h)
  total_hours = Column(Float, nullable=True)              # 工时合计（可手动覆盖）
  verified_hours = Column(Float, nullable=True)           # 核对工时
  hourly_rate = Column(Float, nullable=True)              # 时薪
  amount = Column(Float, nullable=True)                   # 金额
  notes = Column(String, nullable=True)                   # 备注
  source_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
  created_at = Column(DateTime(timezone=True), server_default=func.now())

class Message(Base):
  __tablename__ = "messages"

  id = Column(Integer, primary_key=True, index=True)
  sender = Column(String, nullable=True)       # 发消息的管工
  sender_id = Column(String, nullable=True)    # 企业微信用户ID
  content = Column(Text, nullable=False)       # 原始消息内容
  raw_payload = Column(Text, nullable=True)    # 完整原始请求体（调试用）
  received_at = Column(DateTime(timezone=True), server_default=func.now())
  processed = Column(Boolean, default=False)   # 是否已被处理
