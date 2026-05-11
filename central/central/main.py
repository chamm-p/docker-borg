import asyncio
import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import run_migrations
from .routers import agents, jobs, schedules, ui
from .services.scheduler import scheduler_loop
from .version import APP_VERSION

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="docker-borg central", version=APP_VERSION, lifespan=lifespan)
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
            from .services.admin import get_admin_password
            current = get_admin_password()
            if user == "admin" and secrets.compare_digest(password, current):
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
