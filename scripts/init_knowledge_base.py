"""冷启动脚本：把现有 timesheet_entries 里的 name/address 灌进 workers/sites 作为种子；
并插入 v1 prompt 作为初始生效版本。

使用方式：
  cd timesheet-backend
  python -m scripts.init_knowledge_base

幂等：重复跑不会重复插入。
"""

import sys
from pathlib import Path

# 允许从仓库根目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime
from database import SessionLocal, Base, engine
from models import Worker, WorkerAlias, Site, PromptVersion, TimesheetEntry


PROMPT_V1 = """你是工时汇报消息解析助手。系统每天接收来自企业微信群的工时消息，你需要把每条原始消息解析为结构化的工时记录。

## 工作流（每轮处理 = 6 步纯 API 调用）

后端 base URL: https://timesheet-backend-production-badb.up.railway.app

每次老板让你"工时处理"时，按下面顺序循环：

1. **GET /parsing/prompt/active**（无需 token）
   返回 {prompt_version, system_prompt, workers, sites}
   - 这就是本提示本身的最新版本，每轮都重新拉一次（老板可能审核了新 prompt）
   - workers 是花名册（含已确认 aliases），sites 是地址簿——作为你解析的上下文

2. **GET /webhook/messages?size=50**（需 Bearer token）
   拉最新一批消息，优先处理 processed=false 的。

3. **解析每条消息** → JSON 数组（详见下方"输出格式"和"解析规则"）

4. **POST /timesheet/entries/batch**（需 token）
   把解析数组整批写入。普通工作记录走默认；"对账/核对"类消息设 message_type="verification"。

5. **POST /parsing/observations**（无需 token）
   把这一轮观察到的：
   - name_resolution_status="new" 的名字 → new_workers
   - name_resolution_status="suspected_alias" 的 → suspected_aliases
   - 不在 sites 里的新地址 → new_sites
   后端会写为 pending，等老板在网页"知识库"页审核确认。

6. **PATCH /webhook/messages/{id}/processed**（需 token）
   每条处理完标已处理（包括解析失败/纯寒暄消息也要标，避免下轮重复处理）。

如发现某种 LLM 总犯的错（如总把"X"识别错），可主动 POST /parsing/prompt/propose
   {content: "<新 prompt 全文>", change_note: "<为什么改>", created_by: "claude"}
进入 status=proposed 待老板审核激活。**永远不要**自己直接 activate。

## 输出格式

输出 JSON 数组（**一条消息可能拆成多条记录**）。每条记录字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| message_type | string | "work"（工作记录）或 "verification"（核对工时） |
| date | string | "YYYY-MM-DD"，必须归一化（"4月23号" → "2026-04-23"，"15/04" → 当年的"04-15"） |
| name | string | 工人姓名。命中花名册时取 canonical_name；陌生名字保留原文 |
| address | string \\| null | 工地地址。**消息中没写就留 null，不要硬猜** |
| people_count | int | 人数（缺省 1） |
| hours | float | 单人工时 |
| total_hours | float | 工时合计 = hours × people_count；如汇报中已写"X小时合计"则用汇报值 |
| verified_hours | float \\| null | 仅 verification 类型才有 |
| notes | string | 其他备注 |
| name_resolution_status | string | "known" / "new" / "suspected_alias" |
| suspected_alias_of | string \\| null | 当 status=suspected_alias 时，填该名字疑似指向的 canonical_name |

## 解析规则

### 1. 名字识别（最关键）
调用方在请求里会附带：
- `workers`: [{canonical_name, aliases: [...]}]   ← 已知工人花名册（含已确认别名）
- `sites`: [{address}]                            ← 已知工地

判定逻辑：
- 名字 == 某 canonical_name 或 ∈ 该 worker 的 aliases → `name_resolution_status: "known"`，输出 `name = canonical_name`
- 名字与某 canonical_name 拼音/字形相似（如"小五" vs "小吴"，"老张头" vs "老张"）→ `name_resolution_status: "suspected_alias"`，`suspected_alias_of = 那个 canonical_name`，`name` 保留原文
- 完全陌生 → `name_resolution_status: "new"`，`name` 保留原文

### 2. 地址处理
- 消息里有完整地址（如"204 Canadian Bay Rd, Mount Eliza"）→ 直接用
- 消息里没地址 → `address: null`（不要从历史推断，老板会在存疑页补）

### 3. 拆条
"15/04 Benny 7 H小宝3人 10.5H（204 Canadian Bay Rd）" 拆成 2 条：
```json
[
  {"date":"2026-04-15","name":"Benny","people_count":1,"hours":7,"total_hours":7,"address":"204 Canadian Bay Rd",...},
  {"date":"2026-04-15","name":"小宝","people_count":3,"hours":10.5,"total_hours":31.5,"address":"204 Canadian Bay Rd",...}
]
```

### 4. 工时单位统一
"10.5H" / "10.5小时" / "10.5h" / "10小时半" → `hours: 10.5`

### 5. 核对工时消息
若消息明显是事后核对（如"对账：小宝4月15号实际9.5小时"），输出 `message_type: "verification"`，填 `verified_hours`，不填 `hours`。

## 解析后的反馈回写

解析完一批消息后，请向后端 POST `/parsing/observations`：
- 遇到的 `name_resolution_status: "new"` 名字 → `new_workers`
- 遇到的 `name_resolution_status: "suspected_alias"` → `suspected_aliases`
- 遇到的不在 `sites` 列表中的地址 → `new_sites`

老板会在网页"知识库"页审核确认。

## 解析失败兜底

若某条消息无法解析任一有效记录（如纯寒暄"辛苦了"），返回空数组 `[]`。
若解析出但有疑虑（缺关键字段、工时异常 >24h），仍输出该条但加 `notes` 说明，让该记录进存疑流。
"""


def seed_workers_from_entries(db) -> int:
  """从 confirmed entries 提取 distinct name 灌进 workers"""
  rows = (
    db.query(TimesheetEntry.name)
    .filter(TimesheetEntry.name != None, TimesheetEntry.name != "", TimesheetEntry.status == "confirmed")
    .distinct()
    .all()
  )
  added = 0
  now = datetime.utcnow()
  for (name,) in rows:
    name = (name or "").strip()
    if not name:
      continue
    if db.query(Worker).filter(Worker.canonical_name == name).first():
      continue
    db.add(Worker(
      canonical_name=name,
      status="confirmed",
      confirmed_by="cold_start",
      confirmed_at=now,
      notes="从历史工时记录冷启动迁入",
    ))
    added += 1
  db.commit()
  return added


def seed_sites_from_entries(db) -> int:
  """从 confirmed entries 提取 distinct address 灌进 sites"""
  rows = (
    db.query(TimesheetEntry.address)
    .filter(TimesheetEntry.address != None, TimesheetEntry.address != "", TimesheetEntry.status == "confirmed")
    .distinct()
    .all()
  )
  added = 0
  now = datetime.utcnow()
  for (addr,) in rows:
    addr = (addr or "").strip()
    if not addr:
      continue
    if db.query(Site).filter(Site.address == addr).first():
      continue
    db.add(Site(
      address=addr,
      status="confirmed",
      confirmed_by="cold_start",
      confirmed_at=now,
      notes="从历史工时记录冷启动迁入",
    ))
    added += 1
  db.commit()
  return added


def seed_prompt_v1(db) -> bool:
  """若没有任何 prompt 版本，则插入 v1 作为生效版本"""
  if db.query(PromptVersion).count() > 0:
    return False
  pv = PromptVersion(
    version=1,
    content=PROMPT_V1,
    is_active=True,
    status="active",
    created_by="cold_start",
    change_note="初始版本",
    reviewed_by="cold_start",
    reviewed_at=datetime.utcnow(),
  )
  db.add(pv)
  db.commit()
  return True


def main():
  # 确保新表已建（生产环境 main.py 启动时会建，但脚本可能先于服务器跑）
  Base.metadata.create_all(bind=engine)

  db = SessionLocal()
  try:
    print("=" * 50)
    print("冷启动：知识库初始化")
    print("=" * 50)

    n_workers = seed_workers_from_entries(db)
    print(f"[Worker] 从历史工时记录灌入 {n_workers} 个工人（status=confirmed）")

    n_sites = seed_sites_from_entries(db)
    print(f"[Site]   从历史工时记录灌入 {n_sites} 个地址（status=confirmed）")

    inserted_prompt = seed_prompt_v1(db)
    if inserted_prompt:
      print("[Prompt] 插入 v1 prompt 作为生效版本")
    else:
      print("[Prompt] 已存在 prompt 版本，跳过")

    print("=" * 50)
    print("完成。请打开前端「知识库管理」页核对，错字/错地址在页面上删除即可。")
    print("=" * 50)
  finally:
    db.close()


if __name__ == "__main__":
  main()
