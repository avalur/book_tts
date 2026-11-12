from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import SAMPLE_RATE


def run_ffmpeg(arguments: list[str], timeout=3600):
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", *arguments],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise ValueError("Установите FFmpeg (на macOS: brew install ffmpeg)") from error
    except subprocess.CalledProcessError as error:
        raise ValueError("FFmpeg: " + error.stderr.decode(errors="replace")[-1000:]) from error


def inspect_audio(path: Path, text: str) -> tuple[float, list[str]]:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    if rate != SAMPLE_RATE or audio.shape[1] != 1 or not audio.size or not np.isfinite(audio).all():
        raise ValueError("Некорректный аудиофрагмент")
    duration = len(audio) / rate
    if duration < 0.1 or np.max(np.abs(audio)) < 0.0001:
        raise ValueError("Пустой или беззвучный аудиофрагмент")
    warnings = []
    if np.mean(np.abs(audio) >= 0.999) > 0.001:
        warnings.append("Возможна перегрузка звука")
    # Broad heuristic only, not a word-completeness guarantee.
    if len(text) / duration > 35 or len(text) / duration < 2:
        warnings.append("Необычная длительность — проверьте пропуски и повторы")
    frame = rate // 10
    padded = audio[: len(audio) // frame * frame].reshape(-1, frame)
    silent = np.max(np.abs(padded), axis=1) < 0.002 if len(padded) else []
    longest = count = 0
    for value in silent:
        count = count + 1 if value else 0
        longest = max(longest, count)
    if longest >= 35:
        warnings.append("Тишина дольше 3.5 секунд")
    return duration, warnings


def canonicalize(source: Path, destination: Path):
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        180,
    )


def join_wav(parts: list[tuple[Path, float]], destination: Path) -> float:
    total_frames = sum(sf.info(path).frames + round(pause * SAMPLE_RATE) for path, pause in parts)
    # Standard RIFF has a 4 GiB limit. RF64 keeps long audiobooks streamable.
    format_name = "RF64" if total_frames * 2 > 4_000_000_000 else "WAV"
    with sf.SoundFile(
        destination, "w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16", format=format_name
    ) as output:
        for path, pause in parts:
            with sf.SoundFile(path) as source:
                if (source.channels, source.subtype, source.samplerate) != (
                    1,
                    "PCM_16",
                    SAMPLE_RATE,
                ):
                    raise ValueError("Фрагменты имеют разные форматы")
                while len(frames := source.read(SAMPLE_RATE * 10, dtype="int16")):
                    output.write(frames)
            output.write(np.zeros(round(pause * SAMPLE_RATE), dtype=np.int16))
    return total_frames / SAMPLE_RATE


def metadata_escape(value: str) -> str:
    for source, target in (("\\", "\\\\"), ("=", "\\="), (";", "\\;"), ("#", "\\#")):
        value = value.replace(source, target)
    return value.replace("\r", " ").replace("\n", " ")


def assemble(book: dict, chunks: list[dict], cache: Path, directory: Path) -> dict:
    directory.mkdir(exist_ok=True, parents=True)
    chapter_titles = {c["id"]: c["title"] for c in book["chapters"]}
    chapters = []
    parts = []
    position_ms = 0
    for chunk in chunks:
        new_chapter = not chapters or chapters[-1]["id"] != chunk["chapter_id"]
        if new_chapter:
            chapters.append(
                {
                    "id": chunk["chapter_id"],
                    "title": chapter_titles[chunk["chapter_id"]],
                    "start": position_ms,
                    "end": position_ms,
                    "parts": [],
                }
            )
        # End each chapter with an explicit pause.
        next_index = len(parts) + 1
        end_of_chapter = (
            next_index == len(chunks) or chunks[next_index]["chapter_id"] != chunk["chapter_id"]
        )
        pause = 1.2 if end_of_chapter else chunk["pause"]
        part = (cache / (chunk["audio_key"] + ".wav"), pause)
        parts.append(part)
        chapters[-1]["parts"].append(part)
        frames = sf.info(part[0]).frames + round(pause * SAMPLE_RATE)
        # Use exact cumulative frames to avoid per-fragment rounding drift.
        position_ms += frames * 1000 / SAMPLE_RATE
        chapters[-1]["end"] = position_ms
    master = directory / "master.wav"
    duration = join_wav(parts, master)
    metadata = [
        ";FFMETADATA1",
        f"title={metadata_escape(book['title'])}",
        f"artist={metadata_escape(book['author'])}",
        f"album={metadata_escape(book['title'])}",
        f"language={book['language']}",
    ]
    for chapter in chapters:
        metadata.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={round(chapter['start'])}",
                f"END={round(chapter['end'])}",
                f"title={metadata_escape(chapter['title'])}",
            ]
        )
    metadata_file = directory / "metadata.txt"
    metadata_file.write_text("\n".join(metadata) + "\n", encoding="utf-8")
    # Two-pass loudness normalization across the complete book.
    measurement = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "info",
            "-i",
            str(master),
            "-af",
            "loudnorm=I=-19:TP=-2:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=3600,
    )
    start = measurement.stderr.rfind("{")
    values, _ = json.JSONDecoder().raw_decode(measurement.stderr[start:])
    norm = (
        "loudnorm=I=-19:TP=-2:LRA=11:linear=true:"
        f"measured_I={values['input_i']}:measured_TP={values['input_tp']}:"
        f"measured_LRA={values['input_lra']}:measured_thresh={values['input_thresh']}:"
        f"offset={values['target_offset']}"
    )
    normalized = directory / "normalized.wav"
    run_ffmpeg(
        ["-i", str(master), "-af", norm, "-ar", str(SAMPLE_RATE), "-rf64", "auto", str(normalized)]
    )
    temporary = directory / "audiobook.part.m4b"
    args = ["-i", str(normalized), "-i", str(metadata_file)]
    cover = cache.parent / "books" / book["id"] / "cover.jpg"
    if cover.exists():
        args += ["-i", str(cover)]
    args += [
        "-map",
        "0:a",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "80k",
    ]
    if cover.exists():
        args += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    args += ["-movflags", "+faststart", "-f", "ipod", str(temporary)]
    run_ffmpeg(args)
    temporary.replace(directory / "audiobook.m4b")
    archive_temp = directory / "chapters.part.zip"
    with zipfile.ZipFile(archive_temp, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, chapter in enumerate(chapters):
            mp3 = directory / "chapter.mp3"
            run_ffmpeg(
                [
                    "-ss",
                    str(chapter["start"] / 1000),
                    "-i",
                    str(normalized),
                    "-t",
                    str((chapter["end"] - chapter["start"]) / 1000),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "96k",
                    "-metadata",
                    f"title={chapter['title']}",
                    "-metadata",
                    f"artist={book['author']}",
                    "-metadata",
                    f"album={book['title']}",
                    "-metadata",
                    f"track={index + 1}",
                    str(mp3),
                ]
            )
            archive.write(mp3, f"{index + 1:03d}.mp3")
            mp3.unlink()
    archive_temp.replace(directory / "chapters.zip")
    master.unlink()
    normalized.unlink()
    return {
        "duration": duration,
        "chapters": [{k: v for k, v in c.items() if k != "parts"} for c in chapters],
    }
