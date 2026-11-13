import io
import json
import shutil
import zipfile
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from book_tts.app import create_app
from book_tts.importers import import_book
from book_tts.pdf_import import (
    PdfLine,
    PdfPage,
    chapters_from_pages,
    extract_pages,
    join_lines,
    parse_tsv,
    strip_margins,
)
from book_tts.text_export import export_text


def line(text, y, x=0.1, confidence=98):
    return PdfLine(text, x, y, 0.9, y + 0.02, confidence)


def digital_pdf():
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(500, 700))
    pdf.setTitle("A test book")
    pdf.setAuthor("Test Author")
    for chapter in range(1, 4):
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 670, "A TEST BOOK")
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(40, 580, f"Chapter {chapter}")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(40, 520, "This is a paragraph about a quiet library and a book.")
        pdf.drawString(40, 502, "These wrapped lines belong to the same paragraph.")
        pdf.drawString(55, 460, "A new paragraph begins here.")
        pdf.drawString(240, 20, str(chapter))
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def test_digital_pdf_extracts_chapters_without_ocr_and_strips_running_text(tmp_path):
    book = import_book(digital_pdf(), "test.pdf", cache=tmp_path, ocr="never")
    assert book.title == "A test book" and book.author == "Test Author"
    assert book.language == "en"
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert all("A TEST BOOK" not in c.text for c in book.chapters)
    assert book.import_report["ocr_pages"] == 0
    assert len(book.import_report["removed_margins"]) == 6
    assert "library and a book. These wrapped" in book.chapters[0].text
    assert "\n\nA new paragraph" in book.chapters[0].text


def test_auto_mode_does_not_require_ocr_for_empty_digital_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("book_tts.pdf_import.shutil.which", lambda name: None)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.showPage()
    pdf.setFont("Helvetica", 12)
    pdf.drawString(40, 700, "Chapter 1")
    pdf.drawString(40, 650, "This paragraph is long enough to be readable as ordinary text.")
    pdf.showPage()
    pdf.save()
    book = import_book(buffer.getvalue(), "text.pdf", cache=tmp_path)
    assert book.import_report["ocr_pages"] == 0
    assert "ordinary text" in book.chapters[-1].text


def test_mid_page_chapters_parts_and_front_matter_are_not_lost():
    pages = [
        PdfPage(
            1, [line("Contents", 0.1), line("Chapter 1 .... 3", 0.2), line("Chapter 2 .... 8", 0.3)]
        ),
        PdfPage(
            2,
            [
                line("Book One", 0.1),
                line("A QUIET BEGINNING", 0.16),
                line("1", 0.24),
                line("First chapter begins with a long paragraph. " * 5, 0.34),
                line("2", 0.62),
                line("Second chapter starts on this same physical page. " * 4, 0.72),
            ],
        ),
        PdfPage(
            3,
            [
                line("The previous chapter continues here. " * 5, 0.1),
                line("Book Two", 0.4),
                line("A NEW PART", 0.47),
                line("3", 0.55),
                line("Third chapter begins below its part heading. " * 4, 0.65),
            ],
        ),
    ]
    book = chapters_from_pages(pages, {"ocr_language": "eng"}, "book.pdf")
    assert len(book.chapters) == 4 and not book.chapters[0].included
    assert book.import_report["chapter_count"] == 3
    assert "Second chapter" not in book.chapters[1].text
    assert "previous chapter continues" in book.chapters[2].text
    assert "Book Two" not in book.chapters[2].text
    assert book.chapters[3].text.startswith("Book Two — A NEW PART\n\nChapter 3")
    spans = book.import_report["chapters"]
    assert spans[0]["source_line_start"] == 0
    assert spans[-1]["source_line_end"] == book.import_report["retained_line_count"]
    assert all(
        a["source_line_end"] == b["source_line_start"]
        for a, b in zip(spans, spans[1:], strict=False)
    )


def test_cover_noise_and_footer_numbers_do_not_become_chapters():
    pages = [
        PdfPage(1, [line("1", 0.16), line("cover noise " * 30, 0.3)], "ocr", 30),
        PdfPage(
            2, [line("1", 0.15), line("Actual content " * 30, 0.3), line("7", 0.96)], "ocr", 95
        ),
        PdfPage(
            3, [line("2", 0.2), line("More actual content " * 30, 0.3), line("8", 0.96)], "ocr", 95
        ),
    ]
    book = chapters_from_pages(pages, {"ocr_pages": 3, "ocr_language": "eng"}, "book.pdf")
    assert book.import_report["chapter_count"] == 2
    assert book.import_report["chapters"][1]["pdf_start_page"] == 2
    assert not book.chapters[0].included


def test_missing_heading_reports_a_gap_instead_of_inventing_text():
    pages = [
        PdfPage(1, [line("1", 0.2), line("First chapter text " * 20, 0.3)]),
        PdfPage(2, [line("3", 0.2), line("Third chapter text " * 20, 0.3)]),
    ]
    book = chapters_from_pages(pages, {}, "book.pdf")
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 3"]
    assert book.import_report["warnings"]


def test_no_reliable_headings_preserves_text_as_one_section():
    pages = [PdfPage(1, [line("An unnumbered essay about ordinary things. " * 10, 0.2)])]
    book = chapters_from_pages(pages, {}, "essay.pdf")
    assert len(book.chapters) == 1 and "ordinary things" in book.chapters[0].text
    assert book.import_report["warnings"]


def test_reflow_handles_page_breaks_indents_and_conservative_hyphenation():
    changes = []
    result = join_lines(
        [
            (1, line("Useful infor-", 0.7)),
            (2, line("mation remains available.", 0.1)),
            (2, line("A long-", 0.12)),
            (2, line("term plan.", 0.14)),
            (2, line("A new paragraph.", 0.18, x=0.14)),
        ],
        changes,
    )
    assert "information" in result and "long-term" in result
    assert result.endswith("\n\nA new paragraph.")
    assert len(changes) == 2


def test_tsv_keeps_low_confidence_words_instead_of_silently_dropping_them():
    text = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    text += "1\t1\t0\t0\t0\t0\t0\t0\t1000\t1500\t-1\t\n"
    text += "5\t1\t1\t1\t1\t1\t100\t200\t80\t30\t20\tUncertain\n"
    text += "5\t1\t1\t1\t1\t2\t200\t200\t80\t30\t95\tword\n"
    page = parse_tsv(text, 12)
    assert page.number == 12 and page.lines[0].text == "Uncertain word"
    assert page.confidence < 95 and page.lines[0].x == 0.1


def test_footer_noise_uses_consistent_pagination_without_removing_chapter_numbers():
    pages = [
        PdfPage(
            i, [line("1", 0.2), line(f"Last words {i}.", 0.935), line(footer, 0.956)], "ocr", 95
        )
        for i, footer in enumerate(("10", "11", "12", "l3", "14 ."), 1)
    ]
    cleaned, removed = strip_margins(pages)
    assert all(
        [item.text for item in page.lines] == ["1", f"Last words {page.number}."]
        for page in cleaned
    )
    assert len(removed) == 5 and all(item["reason"] == "page_number" for item in removed)


def test_text_export_keeps_excluded_sections_separate(tmp_path):
    book = chapters_from_pages(
        [
            PdfPage(
                1,
                [
                    line("Preface text", 0.1),
                    line("Chapter 1", 0.4),
                    line("A readable test paragraph. " * 10, 0.5),
                ],
            )
        ],
        {},
        "book.pdf",
    )
    files = export_text(asdict(book), tmp_path)
    assert "Preface text" not in files["text.txt"].read_text()
    with zipfile.ZipFile(files["chapters.zip"]) as archive:
        assert any(name.startswith("extras/") for name in archive.namelist())
        data = json.loads(archive.read("chapters.json"))
        assert data["chapters"][0]["included"] is False


def test_pdf_upload_and_text_download(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/books", files={"file": ("test.pdf", digital_pdf(), "application/pdf")}
        )
        assert response.status_code == 201, response.text
        book_id = response.json()["id"]
        page = client.get(f"/books/{book_id}")
        assert page.status_code == 200 and "PDF → текст по главам" in page.text
        text = client.get(f"/books/{book_id}/export/text.txt")
        assert text.status_code == 200 and "Chapter 3" in text.text
        archive = client.get(f"/books/{book_id}/export/chapters.zip")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            assert len([name for name in zipped.namelist() if name.startswith("chapters/")]) == 3


@pytest.mark.skipif(
    not shutil.which("tesseract") or not shutil.which("pdftoppm"), reason="Local OCR tools required"
)
def test_real_ocr_and_page_cache_reuse(tmp_path, monkeypatch):
    image = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    draw.text((100, 180), "Chapter 1", font=font, fill="black")
    for i, text in enumerate(
        [
            "A quiet library opens every morning.",
            "Readers find their favorite books here.",
            "Every page has a story worth hearing.",
            "This is a local optical recognition test.",
        ]
    ):
        draw.text((100, 380 + i * 60), text, font=font, fill="black")
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(400, 533))
    pdf.drawImage(ImageReader(image), 0, 0, width=400, height=533)
    pdf.showPage()
    pdf.save()
    pages, info = extract_pages(buffer.getvalue(), tmp_path)
    assert info["ocr_pages"] == 1
    assert any("library" in item.text for item in pages[0].lines)

    def should_not_run(*args, **kwargs):
        raise AssertionError("Saved pages must be reused without OCR")

    monkeypatch.setattr("book_tts.pdf_import.ocr_page", should_not_run)
    again, _ = extract_pages(buffer.getvalue(), tmp_path)
    assert asdict(again[0]) == asdict(pages[0])
