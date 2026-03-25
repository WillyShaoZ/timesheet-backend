from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import Base, engine
from routers import webhook

# 启动时自动建表
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Timesheet Backend")

app.add_middleware(
  CORSMiddleware,
  allow_origins=["https://timesheet-frontend-production-10ea.up.railway.app"],
  allow_methods=["*"],
  allow_headers=["*"],
)

app.include_router(webhook.router, prefix="/webhook")

@app.get("/")
def health_check():
  return {"status": "running"}
