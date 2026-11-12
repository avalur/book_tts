from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from .config import Settings

KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
}
KOKORO_VOICES = [
    "af_heart",
    "af_bella",
    "af_sarah",
    "af_nicole",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bm_george",
]


class KokoroSession:
    """Match speed to the actual ONNX export; kokoro-onnx 0.4.9 casts it to int32."""

    def __init__(self, session):
        self.session = session
        self.speed = 1.0

    def get_inputs(self):
        return self.session.get_inputs()

    def run(self, names, inputs):
        speed_input = next(i for i in self.get_inputs() if i.name == "speed")
        dtype = np.float32 if speed_input.type == "tensor(float)" else np.int32
        return self.session.run(names, {**inputs, "speed": np.array([self.speed], dtype=dtype)})


@lru_cache(maxsize=16)
def file_digest(path: str, size: int, modified: int) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest(path: Path):
    stat = path.stat()
    return file_digest(str(path), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=1)
def mac_voices() -> list[dict]:
    if platform.system() != "Darwin" or not shutil.which("say"):
        return []
    result = subprocess.run(
        ["say", "-v", "?"], capture_output=True, text=True, check=True, timeout=20
    )
    voices = []
    for line in result.stdout.splitlines():
        match = re.match(r"(.+?)\s+(en|ru)_\w+\s+#", line)
        if match:
            name, language = match.groups()
            voices.append({"id": name.strip(), "language": language, "label": name.strip()})
    return voices


class Engines:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.kokoro = None

    def catalog(self, remote=True):
        installed = importlib.util.find_spec("kokoro_onnx") is not None
        present = all((self.settings.models / filename).is_file() for filename in KOKORO_FILES)
        catalog = [
            {
                "id": "kokoro",
                "label": "Kokoro · локально",
                "languages": ["en"],
                "ready": installed and present,
                "hint": "Готов"
                if installed and present
                else "Установите extra kokoro и выполните book-tts models download",
                "voices": [{"id": v, "language": "en", "label": v} for v in KOKORO_VOICES],
            }
        ]
        voices = mac_voices()
        catalog.append(
            {
                "id": "macos",
                "label": "macOS · системный голос",
                "languages": ["ru", "en"],
                "ready": bool(voices),
                "hint": "Базовый голос для проверки процесса; качество отличается от нейромоделей",
                "voices": voices,
            }
        )
        if remote and self.settings.remote_url:
            try:
                with httpx.Client(timeout=10) as client:
                    response = client.get(
                        self.settings.remote_url + "/catalog", headers=self.headers()
                    )
                    response.raise_for_status()
                    for engine in response.json():
                        if engine["id"] not in {"qwen", "chatterbox"}:
                            continue
                        engine["id"] = "remote-" + engine["id"]
                        engine["label"] += " · worker"
                        catalog.append(engine)
            except (httpx.HTTPError, ValueError, KeyError):
                catalog.append(
                    {
                        "id": "remote",
                        "label": "Удалённый worker",
                        "languages": ["ru", "en"],
                        "ready": False,
                        "hint": "Worker недоступен: проверьте адрес, подключение и токен",
                        "voices": [],
                    }
                )
        return catalog

    def headers(self):
        return (
            {"Authorization": "Bearer " + self.settings.remote_token}
            if self.settings.remote_token
            else {}
        )

    def resolve(self, profile: dict, language: str) -> dict:
        entry = next(
            (
                e
                for e in self.catalog(remote=profile["engine"].startswith("remote-"))
                if e["id"] == profile["engine"]
            ),
            None,
        )
        if entry is None or not entry["ready"]:
            raise ValueError(entry["hint"] if entry else "Движок не настроен")
        if language not in entry["languages"]:
            raise ValueError("Этот движок не поддерживает язык книги")
        if not any(
            v["id"] == profile["voice"] and v["language"] == language for v in entry["voices"]
        ):
            raise ValueError("Выберите доступный голос для языка книги")
        result = {**profile, "language": language}
        if profile["engine"] == "kokoro":
            result["fingerprint"] = (
                "kokoro-onnx:"
                + importlib.metadata.version("kokoro-onnx")
                + ":"
                + ":".join(digest(self.settings.models / name) for name in KOKORO_FILES)
            )
        elif profile["engine"] == "macos":
            result["fingerprint"] = "macos-say:" + platform.mac_ver()[0]
        else:
            result["fingerprint"] = entry["fingerprint"]
        return result

    def synthesize(self, profile: dict, text: str, destination: Path):
        engine = profile["engine"]
        if engine == "kokoro":
            if profile["language"] != "en":
                raise ValueError("Kokoro в этом приложении используется только для английского")
            current = self.resolve(profile, profile["language"])
            if current["fingerprint"] != profile["fingerprint"]:
                raise ValueError("Версия модели изменилась. Создайте новое задание")
            if self.kokoro is None:
                from kokoro_onnx import Kokoro

                self.kokoro = Kokoro(*(str(self.settings.models / name) for name in KOKORO_FILES))
                self.kokoro.sess = KokoroSession(self.kokoro.sess)
            self.kokoro.sess.speed = profile["speed"]
            samples, rate = self.kokoro.create(
                text,
                voice=profile["voice"],
                speed=profile["speed"],
                lang="en-gb" if profile["voice"].startswith("b") else "en-us",
            )
            sf.write(destination, samples, rate, subtype="PCM_16")
        elif engine == "macos":
            source = destination.with_suffix(".txt")
            aiff = destination.with_suffix(".aiff")
            try:
                source.write_text(text, encoding="utf-8")
                subprocess.run(
                    [
                        "say",
                        "-v",
                        profile["voice"],
                        "-r",
                        str(round(175 * profile["speed"])),
                        "-f",
                        str(source),
                        "-o",
                        str(aiff),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(aiff),
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        str(destination),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
            finally:
                source.unlink(missing_ok=True)
                aiff.unlink(missing_ok=True)
        elif engine.startswith("remote-"):
            with httpx.Client(timeout=httpx.Timeout(900, connect=15)) as client:
                with client.stream(
                    "POST",
                    self.settings.remote_url + "/synthesize",
                    headers=self.headers(),
                    json={
                        "engine": engine.removeprefix("remote-"),
                        "voice": profile["voice"],
                        "language": profile["language"],
                        "speed": profile["speed"],
                        "text": text,
                        "fingerprint": profile["fingerprint"],
                    },
                ) as response:
                    if response.status_code != 200:
                        raise ValueError(
                            f"Worker вернул ошибку {response.status_code}. Подробности в логе worker"
                        )
                    if response.headers.get("X-Model-Fingerprint") != profile["fingerprint"]:
                        raise ValueError("Модель worker изменилась. Создайте новое задание")
                    size = 0
                    with destination.open("wb") as output:
                        for data in response.iter_bytes():
                            size += len(data)
                            if size > 30 * 1024 * 1024:
                                raise ValueError("Worker вернул слишком большой фрагмент")
                            output.write(data)
        else:
            raise ValueError("Неизвестный движок")


def audio_key(profile, text: str, nonce: str = "") -> str:
    payload = {"profile": profile, "text": text, "nonce": nonce, "pipeline": 1}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def download_models(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name, url in KOKORO_FILES.items():
            destination = directory / name
            if destination.is_file():
                print(f"Уже есть: {name}")
                continue
            temporary = destination.with_suffix(".download")
            try:
                print(f"Загрузка {name}…", flush=True)
                with client.stream("GET", url) as response, temporary.open("wb") as output:
                    response.raise_for_status()
                    for block in response.iter_bytes(1024 * 1024):
                        output.write(block)
                if temporary.stat().st_size < 1_000_000:
                    raise ValueError("Получен неполный файл модели")
                temporary.replace(destination)
                print(f"Сохранён {name}: SHA256 {digest(destination)}", flush=True)
            finally:
                temporary.unlink(missing_ok=True)
