from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import Settings
from .importers import Book
from .text import chunk_text, parse_dictionary, prepare_text

ACTIVE = ("queued", "running", "pausing")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS books (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, language TEXT NOT NULL,
 filename TEXT NOT NULL, profile TEXT NOT NULL, dictionary TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, title TEXT NOT NULL, original_text TEXT NOT NULL, text TEXT NOT NULL,
 included INTEGER NOT NULL, is_note INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE, position INTEGER NOT NULL,
 source_start INTEGER NOT NULL, source_end INTEGER NOT NULL, text TEXT NOT NULL,
 speech TEXT NOT NULL, pause REAL NOT NULL, nonce TEXT NOT NULL DEFAULT '',
 audio_key TEXT, duration REAL, warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 profile TEXT NOT NULL, chunk_ids TEXT NOT NULL, created REAL NOT NULL, started REAL,
 finished REAL, error TEXT, stage TEXT NOT NULL DEFAULT 'В очереди',
 done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL,
 synthesis_seconds REAL NOT NULL DEFAULT 0, audio_seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL, started REAL NOT NULL,
 elapsed REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS chunks_book ON chunks(book_id, position);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
"""


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.prepare()
        self.path = settings.data / "library.sqlite3"
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def require_book(db, book_id):
        book = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if book is None:
            raise LookupError("Книга не найдена")
        return dict(book)

    @staticmethod
    def check_idle(db, book_id):
        if db.execute(
            "SELECT 1 FROM jobs WHERE book_id=? AND status IN ('queued','running','pausing')",
            (book_id,),
        ).fetchone():
            raise Conflict("Сначала приостановите или отмените обработку книги")

    @staticmethod
    def rebuild_chunks(db, book_id):
        book = Store.require_book(db, book_id)
        profile = json.loads(book["profile"])
        dictionary = parse_dictionary(book["dictionary"])
        db.execute("DELETE FROM chunks WHERE book_id=?", (book_id,))
        position = 0
        for chapter in db.execute(
            "SELECT * FROM chapters WHERE book_id=? AND included=1 ORDER BY position", (book_id,)
        ).fetchall():
            for chunk in chunk_text(chapter["text"]):
                speech = prepare_text(
                    chunk.text, book["language"], dictionary, profile.get("numbers", False)
                )
                if not speech:
                    raise ValueError("После подготовки получился пустой фрагмент")
                db.execute(
                    """INSERT INTO chunks(book_id, chapter_id, position, source_start,
                    source_end, text, speech, pause) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        book_id,
                        chapter["id"],
                        position,
                        chunk.start,
                        chunk.end,
                        chunk.text,
                        speech,
                        chunk.pause,
                    ),
                )
                position += 1

    def add_book(self, book: Book, filename: str, raw: bytes) -> str:
        book_id = uuid.uuid4().hex
        directory = self.settings.data / "books" / book_id
        directory.mkdir()
        (directory / "original").write_bytes(raw)
        if book.cover:
            (directory / "cover.jpg").write_bytes(book.cover)
        if book.import_report:
            (directory / "import-report.json").write_text(
                json.dumps(book.import_report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        profile = {
            "engine": "kokoro" if book.language == "en" else "macos",
            "voice": "af_heart" if book.language == "en" else "Milena",
            "speed": 1.0,
            "numbers": False,
        }
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO books(id,title,author,language,filename,profile,created) VALUES(?,?,?,?,?,?,?)",
                (
                    book_id,
                    book.title,
                    book.author,
                    book.language,
                    filename,
                    json.dumps(profile),
                    time.time(),
                ),
            )
            for index, chapter in enumerate(book.chapters):
                db.execute(
                    """INSERT INTO chapters(book_id, position, title, original_text, text,
                    included, is_note) VALUES(?,?,?,?,?,?,?)""",
                    (
                        book_id,
                        index,
                        chapter.title,
                        chapter.text,
                        chapter.text,
                        chapter.included,
                        chapter.is_note,
                    ),
                )
            self.rebuild_chunks(db, book_id)
        return book_id

    def list_books(self):
        with self.connect() as db:
            rows = db.execute("""SELECT b.*, (SELECT count(*) FROM chapters c WHERE c.book_id=b.id) chapter_count,
                (SELECT count(*) FROM chunks c WHERE c.book_id=b.id) chunk_count,
                (SELECT status FROM jobs j WHERE j.book_id=b.id AND j.revision=b.revision ORDER BY created DESC LIMIT 1) status
                FROM books b ORDER BY b.created DESC""").fetchall()
        return [dict(r) for r in rows]

    def get_book(self, book_id):
        with self.connect() as db:
            book = self.require_book(db, book_id)
            book["profile"] = json.loads(book["profile"])
            book["chapters"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM chapters WHERE book_id=? ORDER BY position", (book_id,)
                )
            ]
            book["chunks"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM chunks WHERE book_id=? ORDER BY position", (book_id,)
                )
            ]
            book["jobs"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE book_id=? AND revision=? ORDER BY created DESC LIMIT 20",
                    (book_id, book["revision"]),
                )
            ]
        for chunk in book["chunks"]:
            chunk["warnings"] = json.loads(chunk["warnings"])
        book["has_cover"] = (self.settings.data / "books" / book_id / "cover.jpg").is_file()
        report = self.settings.data / "books" / book_id / "import-report.json"
        book["import_report"] = (
            json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
        )
        return book

    def save_book(self, book_id, data: dict):
        with self.connect(write=True) as db:
            book = self.require_book(db, book_id)
            self.check_idle(db, book_id)
            if data["revision"] != book["revision"]:
                raise Conflict("Книга уже изменена в другой вкладке. Обновите страницу")
            stored = {
                r["id"] for r in db.execute("SELECT id FROM chapters WHERE book_id=?", (book_id,))
            }
            if {c["id"] for c in data["chapters"]} != stored or len(data["chapters"]) != len(
                stored
            ):
                raise ValueError("Список глав не совпадает с книгой")
            parse_dictionary(data["dictionary"])
            db.execute(
                """UPDATE books SET title=?,author=?,language=?,profile=?,dictionary=?,
                revision=revision+1 WHERE id=?""",
                (
                    data["title"],
                    data["author"],
                    data["language"],
                    json.dumps(data["profile"]),
                    data["dictionary"],
                    book_id,
                ),
            )
            for chapter in data["chapters"]:
                db.execute(
                    "UPDATE chapters SET title=?,text=?,included=? WHERE id=? AND book_id=?",
                    (
                        chapter["title"],
                        chapter["text"],
                        chapter["included"],
                        chapter["id"],
                        book_id,
                    ),
                )
            db.execute(
                "UPDATE jobs SET status='cancelled',stage='Настройки изменены' WHERE book_id=? AND status='paused'",
                (book_id,),
            )
            self.rebuild_chunks(db, book_id)

    def enqueue(self, book_id, kind: str, profile: dict, revision: int | None = None):
        with self.connect(write=True) as db:
            book = self.require_book(db, book_id)
            self.check_idle(db, book_id)
            if revision is not None and revision != book["revision"]:
                raise Conflict("Книга изменилась во время подготовки задания. Повторите запуск")
            chunks = db.execute(
                "SELECT id,length(speech) length FROM chunks WHERE book_id=? ORDER BY position",
                (book_id,),
            ).fetchall()
            if not chunks:
                raise ValueError("Выберите хотя бы одну непустую главу")
            ids = []
            characters = 0
            for chunk in chunks:
                ids.append(chunk["id"])
                characters += chunk["length"]
                if kind == "preview" and characters >= 900:
                    break
            job_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO jobs(id,book_id,revision,kind,profile,chunk_ids,created,total)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    book_id,
                    book["revision"],
                    kind,
                    json.dumps(profile),
                    json.dumps(ids),
                    time.time(),
                    len(ids),
                ),
            )
        return job_id

    def control(self, job_id, action):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("Задание не найдено")
            job = dict(row)
            if action == "pause" and job["status"] in ACTIVE:
                state = "paused" if job["status"] == "queued" else "pausing"
            elif action == "cancel" and job["status"] in (*ACTIVE, "paused", "failed"):
                state = "cancelled"
            elif action == "resume" and job["status"] in ("paused", "failed"):
                book = self.require_book(db, job["book_id"])
                if book["revision"] != job["revision"]:
                    raise Conflict("Настройки изменились. Создайте новое задание")
                self.check_idle(db, job["book_id"])
                state = "queued"
            else:
                raise Conflict("Это действие недоступно для текущего состояния задания")
            db.execute(
                "UPDATE jobs SET status=?,error=NULL,stage=? WHERE id=?",
                (
                    state,
                    {
                        "queued": "В очереди",
                        "pausing": "Остановка после фрагмента",
                        "paused": "Приостановлено",
                        "cancelled": "Отменено",
                    }[state],
                    job_id,
                ),
            )

    def regenerate(self, book_id, chunk_id):
        with self.connect(write=True) as db:
            self.require_book(db, book_id)
            self.check_idle(db, book_id)
            result = db.execute(
                """UPDATE chunks SET nonce=?,audio_key=NULL,duration=NULL,warnings='[]'
                WHERE id=? AND book_id=?""",
                (uuid.uuid4().hex, chunk_id, book_id),
            )
            if not result.rowcount:
                raise LookupError("Фрагмент не найден")
            db.execute("UPDATE books SET revision=revision+1 WHERE id=?", (book_id,))
            db.execute(
                "UPDATE jobs SET status='cancelled',stage='Фрагмент изменён' WHERE book_id=? AND status='paused'",
                (book_id,),
            )

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("Задание не найдено")
            return dict(row)
