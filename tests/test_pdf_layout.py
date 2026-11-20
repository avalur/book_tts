import io
import json

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from book_tts.pdf_import import extract_pages, join_lines
from book_tts.pdf_layout import text_page
from book_tts.pdf_types import TEXT_VERSION


@pytest.mark.parametrize(
    "tail,expected",
    [
        ("n a quiet library, a reader opens a book.", "In a quiet library"),
        ("opened a book in the quiet library.", "I opened a book"),
    ],
)
def test_drop_cap_joins_the_first_word_and_retains_wrapped_lines(tail, expected):
    data = io.BytesIO()
    pdf = canvas.Canvas(data, pagesize=(500, 700))
    pdf.setFont("Helvetica", 48)
    pdf.drawString(70, 490, "I")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(100, 526, tail)
    pdf.drawString(100, 508, "Every following line belongs to the same paragraph.")
    pdf.drawString(100, 490, "The last line still wraps around the large capital.")
    pdf.drawString(85, 466, "A new paragraph begins here.")
    pdf.showPage()
    pdf.save()
    page = text_page(PdfReader(data).pages[0], 1)
    result = join_lines([(1, item) for item in page.lines], [])
    assert result.startswith(expected)
    assert "book. Every following" in result or "library. Every following" in result
    assert "paragraph. The last line" in result
    assert "\n\nA new paragraph" in result
    assert len(page.adjustments) == 1


def test_linked_small_note_marker_is_removed_but_normal_numbers_and_math_remain():
    data = io.BytesIO()
    pdf = canvas.Canvas(data, pagesize=(500, 700))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(70, 500, "Room 42 has a reference")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(220, 506, "7")
    pdf.linkRect("", "notes", (218, 504, 230, 516))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(235, 500, " and an ordinary number 100.")
    pdf.drawString(70, 470, "The exponent remains x")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(215, 476, "2")
    pdf.showPage()
    pdf.bookmarkPage("notes")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(70, 500, "The note is preserved in the source.")
    pdf.showPage()
    pdf.save()
    page = text_page(PdfReader(data).pages[0], 1)
    text = " ".join(item.text for item in page.lines)
    assert "42" in text and "100" in text and "2" in text
    assert "7" not in text
    assert len(page.references) == 1 and page.references[0]["text"] == "7"


def test_font_changes_preserve_word_spaces_and_adjacent_punctuation():
    data = io.BytesIO()
    pdf = canvas.Canvas(data, pagesize=(500, 700))
    x = 70
    for text, font in [
        ("Hello ", "Helvetica"),
        ("world", "Helvetica-Oblique"),
        (", again.", "Helvetica"),
    ]:
        pdf.setFont(font, 12)
        pdf.drawString(x, 500, text)
        x += pdf.stringWidth(text, font, 12)
    pdf.showPage()
    pdf.save()
    page = text_page(PdfReader(data).pages[0], 1)
    assert page.lines[0].text == "Hello world, again."


def test_text_cache_is_rebuilt_when_the_layout_version_changes(tmp_path):
    data = io.BytesIO()
    pdf = canvas.Canvas(data)
    pdf.drawString(60, 700, "An ordinary paragraph that can be extracted without OCR.")
    pdf.showPage()
    pdf.save()
    pages, info = extract_pages(data.getvalue(), tmp_path, ocr="never")
    path = next(tmp_path.glob("*/*.json"))
    cached = json.loads(path.read_text())
    cached["text_version"] = 0
    cached["lines"][0]["text"] = "STALE TEXT"
    path.write_text(json.dumps(cached))
    fresh, _ = extract_pages(data.getvalue(), tmp_path, ocr="never")
    assert fresh[0].text_version == TEXT_VERSION
    assert fresh[0].lines[0].text == pages[0].lines[0].text
    assert info["ocr_pages"] == 0
