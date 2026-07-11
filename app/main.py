"""Bookbarge FastAPI application."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, projects
from .config import settings

APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = db.migrate()
    if applied:
        print(f"applied migrations: {', '.join(applied)}", flush=True)
    yield


app = FastAPI(title="Bookbarge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

auth.templates = templates
projects.templates = templates
app.include_router(auth.router)
app.include_router(projects.router)


@app.exception_handler(auth._RedirectToLogin)
async def redirect_to_login(request: Request, exc: auth._RedirectToLogin):
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    if auth.current_user(request) is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "index.html")
