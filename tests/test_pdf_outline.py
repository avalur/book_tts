import io

from reportlab.pdfgen import canvas

from book_tts.importers import import_book


def test_bookmarks_preserve_intro_parts_and_conclusion_and_separate_notes(tmp_path):
    data = io.BytesIO()
    pdf = canvas.Canvas(data, pagesize=(500, 700))
    sections = [
        ("Title Page", "Title Page", 0),
        ("Introduction", "Introduction", 0),
        ("Part 1 | FOUNDATIONS", "Part 1", 0),
        ("Chapter 1: An Ordinary Day", "1", 1),
        ("Conclusion", "Conclusion", 0),
        ("Notes", "Notes", 0),
        ("Index", "Index", 0),
        ("Copyright", "Copyright", 0),
    ]
    for index, (title, heading, level) in enumerate(sections):
        key = f"section-{index}"
        pdf.bookmarkPage(key)
        pdf.addOutlineEntry(title, key, level=level)
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(50, 630, heading)
        if heading == "1":
            pdf.drawString(50, 600, "An Ordinary Day")
            pdf.bookmarkPage("child", fit="XYZ", left=50, top=550, zoom=0)
            pdf.addOutlineEntry("A small subsection", "child", level=2)
        elif heading == "Part 1":
            pdf.drawString(50, 600, "FOUNDATIONS")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(50, 500, f"The prose of section {index} is preserved for the reader.")
        pdf.showPage()
    pdf.save()
    book = import_book(data.getvalue(), "outlined.pdf", cache=tmp_path, ocr="never")
    assert book.import_report["segmentation"] == "bookmarks"
    assert [c.title for c in book.chapters if c.included] == [
        "Introduction",
        "Chapter 1: An Ordinary Day",
        "Conclusion",
    ]
    chapter = next(c for c in book.chapters if c.title.startswith("Chapter"))
    assert "FOUNDATIONS" in chapter.text and chapter.text.count("An Ordinary Day") == 1
    assert "section 2" in chapter.text and "section 3" in chapter.text
    assert not any(c.title == "A small subsection" for c in book.chapters)
    notes = next(c for c in book.chapters if c.title == "Notes")
    assert notes.is_note and not notes.included
    assert not next(c for c in book.chapters if c.title == "Index").included
    assert book.import_report["chapter_count"] == 1
    spans = book.import_report["chapters"]
    assert spans[0]["source_line_start"] == 0
    assert spans[-1]["source_line_end"] == book.import_report["retained_line_count"]
    assert all(
        a["source_line_end"] == b["source_line_start"]
        for a, b in zip(spans, spans[1:], strict=False)
    )


def test_bookmark_positions_split_two_chapters_on_one_page(tmp_path):
    data = io.BytesIO()
    pdf = canvas.Canvas(data, pagesize=(500, 700))
    for index, top in ((1, 660), (2, 350)):
        key = f"chapter-{index}"
        pdf.bookmarkPage(key, fit="XYZ", left=40, top=top, zoom=0)
        pdf.addOutlineEntry(f"Chapter {index}: Story {index}", key)
        pdf.setFont("Helvetica", 18)
        pdf.drawString(40, top - 20, f"Chapter {index}: Story {index}")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(
            40, top - 70, f"Only the prose of story number {index} belongs in this chapter."
        )
    pdf.showPage()
    pdf.save()
    book = import_book(data.getvalue(), "same-page.pdf", cache=tmp_path, ocr="never")
    assert len(book.chapters) == 2
    assert "story number 1" in book.chapters[0].text
    assert "story number 2" not in book.chapters[0].text
    assert "story number 2" in book.chapters[1].text
    assert all(
        s["pdf_start_page"] == s["pdf_end_page"] == 1 for s in book.import_report["chapters"]
    )
