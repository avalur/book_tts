from __future__ import annotations

import argparse
import json
import shutil

from .config import Settings
from .engines import Engines, download_models


def main():
    parser = argparse.ArgumentParser(prog="book-tts", description="Локальная мастерская аудиокниг")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Открыть локальный сервис")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("worker", help="Запустить отдельный процесс обработки")
    commands.add_parser("doctor", help="Проверить окружение и движки")
    models = commands.add_parser("models", help="Модели Kokoro")
    models.add_argument("action", choices=["download"])
    remote = commands.add_parser("gpu-worker", help="Worker Qwen3-TTS или Chatterbox")
    remote.add_argument("--engine", choices=["qwen", "chatterbox"], default="qwen")
    remote.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    settings = Settings.load()
    if args.command == "serve":
        import uvicorn

        from .app import create_app

        print(f"Откройте http://127.0.0.1:{args.port}", flush=True)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "models":
        download_models(settings.models)
    elif args.command == "doctor":
        print(
            json.dumps(
                {
                    "ffmpeg": shutil.which("ffmpeg"),
                    "ffprobe": shutil.which("ffprobe"),
                    "data": str(settings.data),
                    "engines": Engines(settings).catalog(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "worker":
        from .worker import main as worker_main

        worker_main()
    elif args.command == "gpu-worker":
        import uvicorn

        from .gpu_worker import create_gpu_app

        uvicorn.run(create_gpu_app(args.engine), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
