from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import router
from app.config import APP_NAME
from app.services.scheduler import schedule_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = Event()
    worker = Thread(target=schedule_loop, args=(stop_event,), name="source-scheduler", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop_event.set()
        worker.join(timeout=2)


app = FastAPI(title=APP_NAME, version="0.3.0", lifespan=lifespan)
app.include_router(router)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME}
