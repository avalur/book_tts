from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data: Path
    models: Path
    remote_url: str = ""
    remote_token: str = ""
    spawn_worker: bool = True

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(Path.cwd() / ".env")
        return cls(
            data=Path(os.getenv("BOOK_TTS_DATA", "data")).expanduser().resolve(),
            models=Path(os.getenv("BOOK_TTS_MODELS", "models")).expanduser().resolve(),
            remote_url=os.getenv("BOOK_TTS_REMOTE_URL", "").rstrip("/"),
            remote_token=os.getenv("BOOK_TTS_REMOTE_TOKEN", ""),
            spawn_worker=os.getenv("BOOK_TTS_WORKER", "1") != "0",
        )

    def prepare(self) -> None:
        for name in ("books", "cache", "jobs"):
            (self.data / name).mkdir(parents=True, exist_ok=True)


MAX_UPLOAD = 40 * 1024 * 1024
MAX_TEXT = 5_000_000
SAMPLE_RATE = 24000
