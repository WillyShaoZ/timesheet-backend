from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, Float, Text, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from database import Base

class User(Base):
  __tablename__ = "users"

  id = Column(Integer, primary_key=True, index=True)
  username = Column(String, unique=True, nullable=False, index=True)
  hashed_password = Column(String, nullable=False)
  role = Column(String, nullable=False, default="viewer")  # boss / accountant
  is_active = Column(Boolean, default=True)

class AuditLog(Base):
  __tablename__ = "audit_logs"

  id = Column(Integer, primary_key=True, index=True)
  username = Column(String, nullable=False)           # 操作人
  action = Column(String, nullable=False)             # CREATE / UPDATE / DELETE
  table_name = Column(String, nullable=False)         # 操作的表
  record_id = Column(Integer, nullable=True)          # 操作的记录ID
  old_values = Column(Text, nullable=True)            # 改前的数据（JSON）
  new_values = Column(Text, nullable=True)            # 改后的数据（JSON）
  created_at = Column(DateTime(timezone=True), server_default=func.now())

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
  status = Column(String, default="confirmed")            # confirmed / pending / rejected
  ai_note = Column(Text, nullable=True)                   # AI 存疑原因
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


# ==================== AI 解析知识库 ====================

class Worker(Base):
  """工人花名册（canonical 名单）+ 员工档案"""
  __tablename__ = "workers"

  id = Column(Integer, primary_key=True, index=True)
  canonical_name = Column(String, unique=True, nullable=False, index=True)  # 标准名
  status = Column(String, default="pending")              # AI 知识库状态: pending / confirmed
  first_seen_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
  confirmed_by = Column(String, nullable=True)            # 由谁确认入库
  confirmed_at = Column(DateTime(timezone=True), nullable=True)
  notes = Column(Text, nullable=True)                     # 老板备注（如真实姓名/工号）
  created_at = Column(DateTime(timezone=True), server_default=func.now())

  # 员工档案字段（HR 维度）
  employment_type = Column(String, default="casual")      # formal / casual / temp
  is_active = Column(Boolean, default=True)               # 在职=True / 离职=False（再回来切回 True）
  default_hourly_rate = Column(Float, nullable=True)      # 默认时薪；新 entry 自动带；改它不影响历史快照
  abn = Column(String, nullable=True)                     # 澳洲税号（formal 类型可能需要）


class WorkerAlias(Base):
  """工人别名/错字映射，alias 可指向多个 canonical（歧义场景）"""
  __tablename__ = "worker_aliases"

  id = Column(Integer, primary_key=True, index=True)
  alias = Column(String, nullable=False, index=True)      # "小五" 等错字/别名
  canonical_id = Column(Integer, ForeignKey("workers.id"), nullable=False, index=True)
  status = Column(String, default="pending")              # pending / auto_resolved / confirmed
  occurrence_count = Column(Integer, default=0)           # 用户确认指向同一 canonical 的累计次数
  first_seen_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
  last_seen_at = Column(DateTime(timezone=True), nullable=True)
  created_at = Column(DateTime(timezone=True), server_default=func.now())

  __table_args__ = (
    UniqueConstraint("alias", "canonical_id", name="uq_worker_alias_canonical"),
  )


class Site(Base):
  """工地地址簿（地址相对固定，不做 alias 子机制）"""
  __tablename__ = "sites"

  id = Column(Integer, primary_key=True, index=True)
  address = Column(String, unique=True, nullable=False, index=True)
  status = Column(String, default="pending")              # pending / confirmed
  first_seen_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
  confirmed_by = Column(String, nullable=True)
  confirmed_at = Column(DateTime(timezone=True), nullable=True)
  notes = Column(Text, nullable=True)
  created_at = Column(DateTime(timezone=True), server_default=func.now())


class PromptVersion(Base):
  """LLM 解析 prompt 版本化（可回滚、可审核）"""
  __tablename__ = "prompt_versions"

  id = Column(Integer, primary_key=True, index=True)
  version = Column(Integer, unique=True, nullable=False)  # 单调递增版本号
  content = Column(Text, nullable=False)                  # system prompt 模板正文
  is_active = Column(Boolean, default=False, index=True)  # 当前生效版本（同时仅 1 条 true）
  status = Column(String, default="proposed")             # proposed / active / superseded / rejected
  created_by = Column(String, nullable=True)              # 'claude' 或用户名
  change_note = Column(Text, nullable=True)               # 这次为什么改
  reviewed_by = Column(String, nullable=True)             # 审核人
  reviewed_at = Column(DateTime(timezone=True), nullable=True)
  created_at = Column(DateTime(timezone=True), server_default=func.now())
