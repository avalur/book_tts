from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import MAX_TEXT, MAX_UPLOAD, Settings
from .engines import Engines
from .importers import import_book
from .store import Conflict, Store
from .text_export import export_text

PACKAGE = Path(__file__).parent


class Profile(BaseModel):
    engine: Literal["kokoro", "macos", "remote-qwen", "remote-chatterbox"]
    voice: str = Field(min_length=1, max_length=100)
    speed: float = Field(default=1, ge=0.7, le=1.4)
    numbers: bool = False


class ChapterEdit(BaseModel):
    id: int
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(max_length=MAX_TEXT)
    included: bool


class BookEdit(BaseModel):
    revision: int
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(max_length=500)
    language: Literal["ru", "en"]
    profile: Profile
    dictionary: str = Field(default="", max_length=100_000)
    chapters: list[ChapterEdit] = Field(max_length=10000)

    @model_validator(mode="after")
    def total_size(self):
        if sum(len(c.text) for c in self.chapters) > MAX_TEXT:
            raise ValueError("Лимит книги — 5 миллионов символов")
        return self


class JobRequest(BaseModel):
    kind: Literal["preview", "render"]


def worker_running(settings):
    lock_path = settings.data / "worker.lock"
    if not lock_path.exists():
        return False
    with lock_path.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    store = Store(settings)
    engines = Engines(settings)
    export_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        process = None
        logfile = None
        if settings.spawn_worker:
            environment = {
                **os.environ,
                "BOOK_TTS_DATA": str(settings.data),
                "BOOK_TTS_MODELS": str(settings.models),
                "BOOK_TTS_REMOTE_URL": settings.remote_url,
                "BOOK_TTS_REMOTE_TOKEN": settings.remote_token,
            }
            logfile = (settings.data / "worker.log").open("a")
            process = subprocess.Popen(
                [sys.executable, "-m", "book_tts.worker"],
                env=environment,
                stdout=logfile,
                stderr=logfile,
                start_new_session=True,
            )
        yield
        if process and process.poll() is None:
            # Terminate the complete process group, including a running say/FFmpeg child.
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                await asyncio.to_thread(process.wait)
        if logfile:
            logfile.close()

    app = FastAPI(title="book_tts", lifespan=lifespan)
    app.state.store = store
    app.state.engines = engines
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlparse(origin)
                if parsed.netloc != request.headers.get("host") or parsed.scheme not in {
                    "http",
                    "https",
                }:
                    return JSONResponse(
                        {"detail": "Недопустимый источник запроса"}, status_code=403
                    )
            elif request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse(
            {"detail": str(error)}, status_code=409 if isinstance(error, Conflict) else 400
        )

    @app.exception_handler(LookupError)
    async def missing(request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.get("/")
    def library(request: Request):
        return templates.TemplateResponse(
            request=request, name="library.html", context={"books": store.list_books()}
        )

    @app.get("/books/{book_id}")
    def book_page(request: Request, book_id: str):
        return templates.TemplateResponse(
            request=request,
            name="book.html",
            context={"book": store.get_book(book_id), "engines": engines.catalog()},
        )

    @app.get("/api/health")
    def health():
        return {"ok": True, "worker": worker_running(settings)}

    @app.get("/api/engines")
    def catalog():
        return engines.catalog()

    @app.post("/api/books", status_code=201)
    async def upload(
        file: UploadFile, ocr_language: str = Form(default="eng", pattern=r"^(eng|rus|eng\+rus)$")
    ):
        raw = bytearray()
        while block := await file.read(1024 * 1024):
            raw.extend(block)
            if len(raw) > MAX_UPLOAD:
                raise HTTPException(413, "Лимит загрузки — 40 МБ")
        filename = Path((file.filename or "book.txt").replace("\\", "/")).name
        imported = await asyncio.to_thread(
            import_book,
            bytes(raw),
            filename,
            cache=settings.data / "pdf-cache",
            language=ocr_language,
        )
        book_id = await asyncio.to_thread(store.add_book, imported, filename, bytes(raw))
        return {"id": book_id, "url": f"/books/{book_id}"}

    @app.get("/api/books/{book_id}")
    def book_info(book_id: str):
        return store.get_book(book_id)

    @app.get("/books/{book_id}/export/{artifact}")
    def text_download(
        book_id: str,
        artifact: Literal["text.txt", "chapters.zip", "chapters.json", "import-report.json"],
    ):
        book = store.get_book(book_id)
        directory = settings.data / "books" / book_id / "text-exports" / str(book["revision"])
        with export_lock:
            if not (directory / "chapters.zip").exists():
                export_text(book, directory)
        return FileResponse(directory / artifact, filename=artifact)

    @app.put("/api/books/{book_id}")
    def edit_book(book_id: str, edit: BookEdit):
        store.save_book(book_id, edit.model_dump())
        return {"revision": store.get_book(book_id)["revision"]}

    @app.get("/api/books/{book_id}/status")
    def status(book_id: str):
        with store.connect() as db:
            book = store.require_book(db, book_id)
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE book_id=? AND revision=? ORDER BY created DESC LIMIT 20",
                    (book_id, book["revision"]),
                )
            ]
            counts = db.execute(
                """SELECT count(*) total, count(audio_key) ready,
                sum(CASE WHEN warnings!='[]' THEN 1 ELSE 0 END) warnings FROM chunks WHERE book_id=?""",
                (book_id,),
            ).fetchone()
        for job in jobs:
            job.pop("profile")
            job.pop("chunk_ids")
            job["rtf"] = (
                round(job["synthesis_seconds"] / job["audio_seconds"], 3)
                if job["audio_seconds"]
                else None
            )
        return {
            "revision": book["revision"],
            "jobs": jobs,
            "chunks": dict(counts),
            "worker": worker_running(settings),
        }

    @app.post("/api/books/{book_id}/jobs", status_code=201)
    def enqueue(book_id: str, data: JobRequest):
        book = store.get_book(book_id)
        profile = engines.resolve(book["profile"], book["language"])
        return {"id": store.enqueue(book_id, data.kind, profile, revision=book["revision"])}

    @app.post("/api/jobs/{job_id}/{action}")
    def control(job_id: str, action: Literal["pause", "resume", "cancel"]):
        store.control(job_id, action)
        return {"ok": True}

    @app.post("/api/books/{book_id}/chunks/{chunk_id}/regenerate")
    def regenerate(book_id: str, chunk_id: int):
        store.regenerate(book_id, chunk_id)
        return {"revision": store.get_book(book_id)["revision"]}

    @app.get("/api/books/{book_id}/chunks")
    def chunks(book_id: str, offset: int = 0, limit: int = 30, warnings: bool = False):
        with store.connect() as db:
            store.require_book(db, book_id)
            rows = db.execute(
                "SELECT * FROM chunks WHERE book_id=? "
                + ("AND warnings!='[]' " if warnings else "")
                + "ORDER BY position LIMIT ? OFFSET ?",
                (book_id, min(max(limit, 1), 100), max(offset, 0)),
            ).fetchall()
        return [{**dict(r), "warnings": json.loads(r["warnings"])} for r in rows]

    @app.get("/audio/{book_id}/{chunk_id}")
    def chunk_audio(book_id: str, chunk_id: int):
        with store.connect() as db:
            row = db.execute(
                "SELECT audio_key FROM chunks WHERE book_id=? AND id=?", (book_id, chunk_id)
            ).fetchone()
        if row is None or not row[0]:
            raise HTTPException(404, "Аудиофрагмент пока не готов")
        path = settings.data / "cache" / (row[0] + ".wav")
        if not path.exists():
            raise HTTPException(404, "Аудиофайл отсутствует. Запустите обработку повторно")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/books/{book_id}/cover")
    def cover(book_id: str):
        store.get_book(book_id)
        path = settings.data / "books" / book_id / "cover.jpg"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/download/{job_id}/{artifact}")
    def download(
        job_id: str,
        artifact: Literal["audiobook.m4b", "chapters.zip", "preview.wav", "manifest.json"],
    ):
        job = store.job(job_id)
        book = store.get_book(job["book_id"])
        if job["status"] != "completed" or book["revision"] != job["revision"]:
            raise HTTPException(409, "Результат ещё не готов или настройки книги изменились")
        path = settings.data / "jobs" / job_id / artifact
        if not path.exists():
            raise HTTPException(404, "Результат отсутствует. Запустите обработку повторно")
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", book["title"])[:120]
        filename = name + Path(artifact).suffix if artifact != "manifest.json" else "manifest.json"
        return FileResponse(
            path,
            filename=filename,
            media_type="audio/wav" if artifact == "preview.wav" else None,
            content_disposition_type="inline" if artifact == "preview.wav" else "attachment",
        )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/favicon.svg")

    return app
