"""Portable UTF-8 text exports; private source text stays in the data directory."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path


def export_text(book: dict, destination: Path) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    report = book.get("import_report") or {}
    ranges = report.get("chapters", [])
    chapters = []
    names = []
    selected = []
    chapter_index = extra_index = 0
    for index, chapter in enumerate(book["chapters"]):
        included = bool(chapter.get("included", True))
        if included:
            chapter_index += 1
        else:
            extra_index += 1
        number = chapter_index if included else extra_index
        slug = (
            re.sub(r"[^\w-]+", "-", chapter["title"], flags=re.UNICODE).strip("-")[:80] or "section"
        )
        folder = "chapters" if included else "extras"
        filename = f"{folder}/{number:03d}-{slug}.txt"
        path = destination / filename
        path.parent.mkdir(exist_ok=True)
        path.write_text(chapter["text"].strip() + "\n", encoding="utf-8")
        names.append(filename)
        if included:
            selected.append(chapter["text"].strip())
        chapters.append(
            {
                "title": chapter["title"],
                "included": included,
                "file": filename,
                "text": chapter["text"],
                "source": ranges[index] if index < len(ranges) else None,
            }
        )
    payload = {
        "title": book["title"],
        "author": book.get("author", ""),
        "language": book.get("language", ""),
        "chapters": chapters,
    }
    files = {
        "text.txt": destination / "text.txt",
        "chapters.json": destination / "chapters.json",
        "import-report.json": destination / "import-report.json",
        "chapters.zip": destination / "chapters.zip",
    }
    files["text.txt"].write_text("\n\n".join(selected) + "\n", encoding="utf-8")
    files["chapters.json"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files["import-report.json"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary = destination / "chapters.part.zip"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in [*names, "text.txt", "chapters.json", "import-report.json"]:
            archive.write(destination / filename, filename)
    temporary.replace(files["chapters.zip"])
    return files
