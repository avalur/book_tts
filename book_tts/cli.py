from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .engines import Engines, download_models


def main():
    parser = argparse.ArgumentParser(prog="book-tts", description="Локальная мастерская аудиокниг")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Открыть локальный сервис")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("worker", help="Запустить отдельный процесс обработки")
    commands.add_parser("doctor", help="Проверить окружение и движки")
    extract = commands.add_parser(
        "extract-pdf", help="Извлечь PDF в текст по главам, с OCR для сканов"
    )
    extract.add_argument("source", type=Path)
    extract.add_argument("--output", "-o", type=Path)
    extract.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    extract.add_argument("--ocr-language", choices=["eng", "rus", "eng+rus"], default="eng")
    extract.add_argument("--workers", type=int, default=4)
    extract.add_argument("--title")
    extract.add_argument("--author")
    extract.add_argument("--import", dest="add_to_library", action="store_true")
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
                    "tesseract": shutil.which("tesseract"),
                    "pdftoppm": shutil.which("pdftoppm"),
                    "data": str(settings.data),
                    "engines": Engines(settings).catalog(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "extract-pdf":
        from .importers import import_book
        from .store import Store
        from .text_export import export_text

        if args.source.suffix.lower() != ".pdf":
            parser.error("Укажите файл .pdf")
        raw = args.source.read_bytes()

        def progress(done, total, page):
            if done % 10 == 0 or done == total:
                print(f"PDF: {done}/{total} страниц сохранено", flush=True)

        book = import_book(
            raw,
            args.source.name,
            cache=settings.data / "pdf-cache",
            ocr=args.ocr,
            language=args.ocr_language,
            workers=args.workers,
            progress=progress,
        )
        if args.title:
            book.title = args.title
        if args.author:
            book.author = args.author
        destination = (args.output or settings.data / "extracted" / args.source.stem).resolve()
        export_text(asdict(book), destination)
        print(f"Готово: {book.import_report['chapter_count']} глав; результаты: {destination}")
        if args.add_to_library:
            book_id = Store(settings).add_book(book, args.source.name, raw)
            print(f"Книга добавлена в библиотеку: http://127.0.0.1:8000/books/{book_id}")
    elif args.command == "worker":
        from .worker import main as worker_main

        worker_main()
    elif args.command == "gpu-worker":
        import uvicorn

        from .gpu_worker import create_gpu_app

        uvicorn.run(create_gpu_app(args.engine), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
