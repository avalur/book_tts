import json
import shutil
import subprocess
import zipfile

import pytest

from book_tts.engines import audio_key
from book_tts.store import Conflict
from book_tts.text import chunk_text, parse_dictionary, prepare_text
from book_tts.worker import Worker


@pytest.mark.parametrize(
    "text",
    [
        "Привет. " * 300,
        "a" * 1800,
        "One sentence.\n\nAnother sentence! " * 40,
        "A.\n\n B.  \n",
        "Большое слово " * 200,
    ],
)
def test_chunks_cover_source_without_gaps(text):
    chunks = chunk_text(text)
    assert "".join(c.text for c in chunks) == text
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.end == b.start


def test_dictionary_is_literal_nonrecursive_and_whole_word():
    dictionary = parse_dictionary("Поттер = Гарри\nГарри = герой\na.b = dot")
    assert (
        prepare_text("Поттер и Гарри, Поттеров, a.b", "ru", dictionary)
        == "Гарри и герой, Поттеров, dot"
    )
    with pytest.raises(ValueError):
        parse_dictionary("incorrect line")


def test_numbers_preserve_dates_and_versions():
    result = prepare_text("42 книги, 12.03.2026 и v1.2, 3,14", "ru", {}, True)
    assert result == "сорок два книги, 12.03.2026 и v1.2, 3,14"


def test_cache_identity_tracks_voice_version_speed_and_nonce(profile):
    key = audio_key(profile, "hello")
    for field, value in [("voice", "other"), ("speed", 1.2), ("fingerprint", "v2")]:
        assert audio_key({**profile, field: value}, "hello") != key
    assert audio_key(profile, "hello", "new-take") != key
    assert audio_key(profile, "different") != key


def test_edits_are_revision_checked_and_blocked_while_running(store, book_id, profile):
    book = store.get_book(book_id)
    job = store.enqueue(book_id, "render", profile)
    with pytest.raises(Conflict):
        store.save_book(book_id, book)
    store.control(job, "pause")
    store.save_book(book_id, book)
    assert store.job(job)["status"] == "cancelled"
    with pytest.raises(Conflict):
        store.save_book(book_id, book)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_pause_resume_and_recover_reuse_completed_audio(
    store, settings, book_id, profile, test_engine
):
    job = store.enqueue(book_id, "preview", profile)
    worker = Worker(settings, test_engine)
    test_engine.after_synthesis = lambda: store.control(job, "pause")
    assert worker.run_once()
    assert store.job(job)["status"] == "paused"
    assert len(test_engine.calls) == 1
    test_engine.after_synthesis = None
    store.control(job, "resume")
    # Simulate a process dying after claiming the job.
    assert worker.claim()["id"] == job
    Worker(settings, test_engine).recover()
    assert store.job(job)["status"] == "queued"
    worker.run_once()
    assert store.job(job)["status"] == "completed"
    assert len(test_engine.calls) == store.job(job)["total"]
    assert (settings.data / "jobs" / job / "preview.wav").exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_full_export_chapter_metadata_manifest_and_regeneration(
    store, settings, book_id, profile, test_engine
):
    job = store.enqueue(book_id, "render", profile)
    worker = Worker(settings, test_engine)
    worker.run_once()
    assert store.job(job)["status"] == "completed", store.job(job)["error"]
    directory = settings.data / "jobs" / job
    probe = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(directory / "audiobook.m4b"),
            "-f",
            "ffmetadata",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.count("[CHAPTER]") == 2
    assert "title=Тестовая книга" in probe.stdout
    assert "title=Глава 1" in probe.stdout and "title=Глава 2" in probe.stdout
    with zipfile.ZipFile(directory / "chapters.zip") as archive:
        assert archive.namelist() == ["001.mp3", "002.mp3"]
    manifest = json.loads((directory / "manifest.json").read_text())
    assert len(manifest["chunks"]) == store.job(job)["total"]
    assert manifest["profile"]["fingerprint"] == profile["fingerprint"]
    calls = len(test_engine.calls)
    first = store.get_book(book_id)["chunks"][0]
    store.regenerate(book_id, first["id"])
    new_job = store.enqueue(book_id, "render", profile)
    worker.run_once()
    assert store.job(new_job)["status"] == "completed", store.job(new_job)["error"]
    assert len(test_engine.calls) == calls + 1


def test_coverage_rejects_missing_or_reordered_chunks(store, book_id):
    book = store.get_book(book_id)
    Worker.verify_coverage(book, book["chunks"])
    with pytest.raises(ValueError):
        Worker.verify_coverage(book, book["chunks"][:-1])
    with pytest.raises(ValueError):
        Worker.verify_coverage(book, list(reversed(book["chunks"])))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_engine_failure_is_bounded_and_preserves_error(
    store, settings, book_id, profile, test_engine
):
    def fail(*args):
        test_engine.calls.append("failure")
        raise ValueError("model unavailable")

    test_engine.synthesize = fail
    job = store.enqueue(book_id, "preview", profile)
    Worker(settings, test_engine).run_once()
    assert store.job(job)["status"] == "failed"
    assert "model unavailable" in store.job(job)["error"]
    assert len(test_engine.calls) == 2


def test_cancelled_job_is_not_claimed(store, settings, book_id, profile, test_engine):
    job = store.enqueue(book_id, "render", profile)
    store.control(job, "cancel")
    assert not Worker(settings, test_engine).run_once()
    assert not test_engine.calls


def test_enqueue_rejects_a_revision_changed_while_resolving_model(store, book_id, profile):
    book = store.get_book(book_id)
    store.save_book(book_id, book)
    with pytest.raises(Conflict):
        store.enqueue(book_id, "preview", profile, revision=book["revision"])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_corrupt_cache_is_regenerated(store, settings, book_id, profile, test_engine):
    first_job = store.enqueue(book_id, "preview", profile)
    worker = Worker(settings, test_engine)
    worker.run_once()
    assert store.job(first_job)["status"] == "completed"
    chunk = store.get_book(book_id)["chunks"][0]
    (settings.data / "cache" / (chunk["audio_key"] + ".wav")).write_bytes(b"broken audio")
    calls = len(test_engine.calls)
    next_job = store.enqueue(book_id, "preview", profile)
    worker.run_once()
    assert store.job(next_job)["status"] == "completed", store.job(next_job)["error"]
    assert len(test_engine.calls) == calls + 1
