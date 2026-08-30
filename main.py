"""入口：组装 FastAPI，挂载前端与媒体目录，初始化数据库。"""
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app import db, config
from app.routers import api

app = FastAPI(title="数字人短视频智能体平台", version="1.0.0")
app.include_router(api)

# 媒体文件（配音/视频/封面/形象/音色）
app.mount("/files", StaticFiles(directory=config.STORAGE_DIR), name="files")
# 前端静态资源
app.mount("/static", StaticFiles(directory=os.path.join(config.STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


@app.on_event("startup")
def _startup():
    db.init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
