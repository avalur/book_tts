"""Use PDF destinations to retain named introductions, chapters and reference sections."""

from __future__ import annotations

import re
from pathlib import Path

from .importers import Book, Chapter


def section_kind(title: str) -> str:
    title = title.strip().casefold()
    if re.match(r"(?:chapter|глава)\s+[\divxlcdm]+\b", title):
        return "chapter"
    if re.match(r"(?:part|book|часть|книга)\s+[\w]+\b", title):
        return "part"
    if re.fullmatch(r"introduction|preface|foreword|prologue|введение|предисловие|пролог", title):
        return "introduction"
    if re.fullmatch(r"conclusion|epilogue|afterword|заключение|эпилог|послесловие", title):
        return "conclusion"
    if re.fullmatch(
        r"notes|endnotes|footnotes|references|bibliography|примечания|сноски|библиография", title
    ):
        return "notes"
    if re.fullmatch(r"index|subject index|предметный указатель|указатель", title):
        return "index"
    if re.fullmatch(
        r"copyright|acknowledg(?:e)?ments|about the author|by the same author|title page|dedication|contents|table of contents",
        title,
    ):
        return "extra"
    return "section"


def normalized(value):
    return re.sub(r"\W", "", value).casefold()


def trim_heading(lines, title, kind):
    """Replace only headings that demonstrably repeat the destination label."""
    if not lines:
        return lines
    target = normalized(title)
    if normalized(lines[0][1].text) == target:
        return lines[1:]
    if kind == "chapter":
        number = re.match(r"(?:chapter|глава)\s+([\divxlcdm]+)", title, re.I)
        first = normalized(lines[0][1].text)
        if number and first in {
            number[1].casefold(),
            "chapter" + number[1].casefold(),
            "глава" + number[1].casefold(),
        }:
            title_rest = normalized(
                re.sub(r"^(?:chapter|глава)\s+[\divxlcdm]+\s*[:.\-–—]?", "", title, flags=re.I)
            )
            consumed = ""
            for index in range(1, min(len(lines), 5)):
                consumed += normalized(lines[index][1].text)
                if consumed == title_rest:
                    return lines[index + 1 :]
                if not title_rest.startswith(consumed):
                    break
            return lines[1:]
    return lines


def bookmarked_book(pages, info, filename, reflow):
    entries = info.get("bookmarks", [])
    known = [section_kind(e["title"]) for e in entries]
    if len(entries) < 2 or not (
        "chapter" in known or ("introduction" in known and "conclusion" in known)
    ):
        return None
    all_lines = [(page.number, line) for page in pages for line in page.lines]
    if not all_lines:
        return None
    records = []
    parent_depth = None
    for entry in entries:
        depth = entry["depth"]
        if parent_depth is not None and depth > parent_depth:
            continue
        kind = section_kind(entry["title"])
        parent_depth = None if kind == "part" else depth
        indices = [
            i
            for i, (number, line) in enumerate(all_lines)
            if number == entry["page"] and line.y >= entry["y"] - 0.015
        ]
        if not indices:
            continue
        start = indices[0]
        if records and start < records[-1]["start"]:
            return None  # Non-monotonic destinations: preserve the heuristic path.
        if records and start == records[-1]["start"] and records[-1]["kind"] != "part":
            continue
        records.append({**entry, "start": start, "heading": start, "kind": kind})
    if len(records) < 2 or not any(
        r["kind"] in {"chapter", "introduction", "conclusion"} for r in records
    ):
        return None
    # Keep a part's prose at the front of its first chapter, rather than creating a
    # tiny audio chapter or accidentally appending it to the previous chapter.
    merged = []
    for record in records:
        if merged and merged[-1]["kind"] == "part" and record["kind"] == "chapter":
            part = merged.pop()
            record = {**record, "start": part["start"], "part": part["title"]}
        merged.append(record)
    if merged[0]["start"] > 0:
        merged.insert(
            0,
            {
                "title": "Перед началом книги",
                "start": 0,
                "heading": 0,
                "kind": "extra",
                "page": all_lines[0][0],
            },
        )
    book = Book(info.get("title") or Path(filename).stem, info.get("author", ""), chapters=[])
    changes, spans = [], []
    active_part = ""
    for i, record in enumerate(merged):
        end = merged[i + 1]["start"] if i + 1 < len(merged) else len(all_lines)
        if end <= record["start"]:
            continue
        source = all_lines[record["start"] : end]
        prefix = all_lines[record["start"] : record["heading"]]
        body = trim_heading(all_lines[record["heading"] : end], record["title"], record["kind"])
        if record.get("part"):
            active_part = record["part"]
        text = (reflow(prefix, changes) + "\n\n" if prefix else "") + record["title"]
        if body:
            text += "\n\n" + reflow(body, changes)
        excluded = record["kind"] in {"notes", "index", "extra", "part"}
        book.chapters.append(
            Chapter(record["title"], text, is_note=record["kind"] == "notes", included=not excluded)
        )
        spans.append(
            {
                "kind": record["kind"],
                "title": record["title"],
                "part": active_part,
                "pdf_start_page": source[0][0],
                "pdf_end_page": source[-1][0],
                "chapter_heading_page": record["page"],
                "source_line_start": record["start"],
                "source_line_end": end,
            }
        )
    book.import_report = {
        "chapter_count": sum(s["kind"] == "chapter" for s in spans),
        "chapters": spans,
        "warnings": [],
        "hyphenation_changes": changes,
        "retained_line_count": len(all_lines),
        "segmentation": "bookmarks",
    }
    return book
