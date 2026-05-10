import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
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

app.include_router(agents.router)
app.include_router(jobs.router)
app.include_router(schedules.router)
app.include_router(ui.router)
