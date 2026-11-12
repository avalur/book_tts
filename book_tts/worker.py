from __future__ import annotations

import fcntl
import json
import logging
import signal
import time

from .audio import assemble, canonicalize, inspect_audio, join_wav
from .config import Settings
from .engines import Engines, audio_key
from .store import Store

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, engines=None):
        self.settings = settings
        self.store = Store(settings)
        self.engines = engines or Engines(settings)
        self.stopping = False

    def recover(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE jobs SET status='queued',stage='Восстановление после прерывания' WHERE status='running'"
            )
            db.execute(
                "UPDATE jobs SET status='paused',stage='Приостановлено' WHERE status='pausing'"
            )
            db.execute(
                "UPDATE attempts SET error='Процесс был прерван' WHERE elapsed IS NULL AND error IS NULL"
            )

    def checkpoint(self, job_id):
        with self.store.connect(write=True) as db:
            status = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            if status == "pausing":
                db.execute(
                    "UPDATE jobs SET status='paused',stage='Приостановлено' WHERE id=?", (job_id,)
                )
                return False
            if self.stopping and status == "running":
                db.execute(
                    "UPDATE jobs SET status='queued',stage='Продолжится при запуске' WHERE id=?",
                    (job_id,),
                )
                return False
            return status == "running"

    def claim(self):
        with self.store.connect(write=True) as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if job is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running',started=coalesce(started,?),stage='Подготовка',error=NULL WHERE id=?",
                (time.time(), job["id"]),
            )
            return dict(job)

    def render_chunk(self, job, chunk, profile):
        key = audio_key(profile, chunk["speech"], chunk["nonce"])
        target = self.settings.data / "cache" / (key + ".wav")
        if target.exists():
            try:
                return key, *inspect_audio(target, chunk["speech"]), 0.0
            except (ValueError, RuntimeError):
                target.unlink(missing_ok=True)
        attempt_error = None
        for _ in range(2):
            if not self.checkpoint(job["id"]):
                return None
            started = time.time()
            with self.store.connect(write=True) as db:
                attempt_id = db.execute(
                    "INSERT INTO attempts(job_id,chunk_id,started) VALUES(?,?,?)",
                    (job["id"], chunk["id"], started),
                ).lastrowid
            raw = target.with_suffix(".raw.wav")
            temporary = target.with_suffix(".part.wav")
            try:
                self.engines.synthesize(profile, chunk["speech"], raw)
                canonicalize(raw, temporary)
                duration, warnings = inspect_audio(temporary, chunk["speech"])
                temporary.replace(target)
                elapsed = time.time() - started
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE attempts SET elapsed=? WHERE id=?", (elapsed, attempt_id))
                    db.execute(
                        "UPDATE jobs SET synthesis_seconds=synthesis_seconds+?,audio_seconds=audio_seconds+? WHERE id=?",
                        (elapsed, duration, job["id"]),
                    )
                return key, duration, warnings, elapsed
            except Exception as error:
                attempt_error = error
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE attempts SET elapsed=?,error=? WHERE id=?",
                        (time.time() - started, str(error)[:1500], attempt_id),
                    )
            finally:
                raw.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
        raise ValueError(f"Не удалось озвучить фрагмент {chunk['position'] + 1}: {attempt_error}")

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        try:
            book = self.store.get_book(job["book_id"])
            if book["revision"] != job["revision"]:
                raise ValueError("Настройки задания устарели")
            ids = json.loads(job["chunk_ids"])
            chunk_map = {c["id"]: c for c in book["chunks"]}
            if not all(i in chunk_map for i in ids):
                raise ValueError("Состав фрагментов изменился")
            chunks = [chunk_map[i] for i in ids]
            profile = json.loads(job["profile"])
            for index, chunk in enumerate(chunks):
                if not self.checkpoint(job["id"]):
                    return True
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE jobs SET stage=? WHERE id=?",
                        (f"Озвучка {index + 1} из {len(chunks)}", job["id"]),
                    )
                result = self.render_chunk(job, chunk, profile)
                if result is None or not self.checkpoint(job["id"]):
                    return True
                key, duration, warnings, _ = result
                chunk.update(audio_key=key, duration=duration, warnings=warnings)
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE chunks SET audio_key=?,duration=?,warnings=? WHERE id=?",
                        (key, duration, json.dumps(warnings, ensure_ascii=False), chunk["id"]),
                    )
                    db.execute("UPDATE jobs SET done=? WHERE id=?", (index + 1, job["id"]))
            if not self.checkpoint(job["id"]):
                return True
            directory = self.settings.data / "jobs" / job["id"]
            directory.mkdir(exist_ok=True)
            with self.store.connect(write=True) as db:
                db.execute("UPDATE jobs SET stage='Сборка аудио' WHERE id=?", (job["id"],))
            if job["kind"] == "preview":
                temporary = directory / "preview.part.wav"
                join_wav(
                    [
                        (self.settings.data / "cache" / (c["audio_key"] + ".wav"), c["pause"])
                        for c in chunks
                    ],
                    temporary,
                )
                temporary.replace(directory / "preview.wav")
                summary = {}
            else:
                self.verify_coverage(book, chunks)
                summary = assemble(book, chunks, self.settings.data / "cache", directory)
            if not self.checkpoint(job["id"]):
                return True
            finished = time.time()
            manifest = {
                "title": book["title"],
                "author": book["author"],
                "language": book["language"],
                "revision": book["revision"],
                "profile": profile,
                "summary": summary,
                "chunks": chunks,
                "job": {
                    **self.store.job(job["id"]),
                    "status": "completed",
                    "stage": "Готово",
                    "finished": finished,
                },
            }
            with self.store.connect() as db:
                manifest["attempts"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM attempts WHERE job_id=? ORDER BY id", (job["id"],)
                    )
                ]
            (directory / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE jobs SET status='completed',stage='Готово',finished=? WHERE id=? AND status='running'",
                    (finished, job["id"]),
                )
        except Exception as error:
            logger.exception("Job %s failed", job["id"])
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE jobs SET status='failed',stage='Нужна проверка',error=?,finished=? WHERE id=? AND status='running'",
                    (str(error)[:2000], time.time(), job["id"]),
                )
        return True

    @staticmethod
    def verify_coverage(book, chunks):
        expected = [c for c in book["chapters"] if c["included"] and c["text"].strip()]
        chapter_order = list(dict.fromkeys(c["chapter_id"] for c in chunks))
        if chapter_order != [c["id"] for c in expected]:
            raise ValueError("Нарушен порядок глав")
        for chapter in expected:
            source = [c for c in chunks if c["chapter_id"] == chapter["id"]]
            position = 0
            for chunk in source:
                if (
                    chunk["source_start"] != position
                    or chapter["text"][position : chunk["source_end"]] != chunk["text"]
                ):
                    raise ValueError("Нарушено покрытие текста фрагментами")
                position = chunk["source_end"]
            if position != len(chapter["text"]):
                raise ValueError("Не весь текст главы включён в аудио")

    def run(self):
        # The OS releases this lock even after a crash. No stale PID heuristics.
        with (self.settings.data / "worker.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.info("Worker already running for this library")
                return
            self.recover()
            heartbeat = self.settings.data / "worker.heartbeat"
            while not self.stopping:
                heartbeat.touch()
                if not self.run_once():
                    time.sleep(0.5)
            heartbeat.unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = Worker(Settings.load())

    def stop(signum, frame):
        worker.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()


if __name__ == "__main__":
    main()
