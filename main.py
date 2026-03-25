from fastapi import FastAPI
from database import Base, engine
from routers import webhook

# 启动时自动建表
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Timesheet Backend")

app.include_router(webhook.router, prefix="/webhook")

@app.get("/")
def health_check():
  return {"status": "running"}
