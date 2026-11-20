from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.discovery import router as discovery_router
from app.api.routes import router
from app.config import APP_NAME
from app.services.discovery import start_discovery_worker, stop_discovery_worker

app = FastAPI(title=APP_NAME, version="0.3.0")
app.include_router(router)
app.include_router(discovery_router)


@app.on_event('startup')
def start_background_discovery_worker():
    start_discovery_worker()


@app.on_event('shutdown')
def stop_background_discovery_worker():
    stop_discovery_worker()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME}
