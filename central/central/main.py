import asyncio
import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .routers import agents, jobs, schedules, ui
from .services.scheduler import scheduler_loop

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="docker-borg central", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="central/static"), name="static")


@app.middleware("http")
async def admin_auth(request: Request, call_next):
    path = request.url.path

    if path.startswith("/api/") or path.startswith("/static/"):
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, password = decoded.split(":", 1)
            if user == "admin" and secrets.compare_digest(password, settings.admin_password):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="docker-borg"'},
        content="Unauthorized",
    )


app.include_router(agents.router)
app.include_router(jobs.router)
app.include_router(schedules.router)
app.include_router(ui.router)
