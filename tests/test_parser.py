"""
app.parser tests. No app.config dependency, so this runs fully offline
regardless of credentials -- same as test_keyword_filter.py.

The previous version of this file only pprint()'d one Nafezly example
for manual inspection, with no assertions at all, so a parsing
regression here would never fail a test run. This keeps that same
example (as a real assertion) and adds the generic/Mostaql fallback
path and a couple of edge cases the parser is supposed to handle.
"""

from app.parser import parse_job


def test_nafezly_style_message_extracts_structured_fields():
    text = """
تم إضافة مشروع جديد على منصة نفذلي

عنوان المشروع : إعادة بناء ملفات PDF احترافية داخل Microsoft Word

تفاصيل المشروع : أبحث عن خبير Microsoft Word محترف جدًا لديه خبرة حقيقية في تصميم المستندات الاحترافية وإعادة بنائها داخل Word.

المشروع ليس تحويل PDF إلى Word.

المطلوب هو إعادة بناء المستند بالكامل داخل Word.

الميزانية : 10 - 25 دولار

رابط المشروع : https://nafezly.com/project/52103
"""

    job = parse_job("Nafezly - نفذلي", text)

    assert job["title"] == "إعادة بناء ملفات PDF احترافية داخل Microsoft Word"
    assert job["budget"] == "10 - 25 دولار"
    assert job["url"] == "https://nafezly.com/project/52103"
    assert job["source"] == "Nafezly - نفذلي"
    # The description is normalized (paragraphs preserved, single
    # newlines collapsed to spaces) and pulled from between "تفاصيل
    # المشروع" and "الميزانية", not the whole raw message.
    assert "أبحث عن خبير Microsoft Word" in job["description"]
    assert "الميزانية" not in job["description"]
    assert "تم إضافة مشروع جديد" not in job["description"]


def test_nafezly_detection_is_case_and_source_independent():
    """
    The Nafezly-specific parsing path is dispatched purely on whether
    "nafezly" (case-insensitively) appears anywhere in the `source`
    string -- see app.parser.parse_job's `source_name = (source or
    "").lower()` check. This is a real, load-bearing use of the
    channel/source string (not just display metadata), which is why
    app.message_processor must keep passing the channel title into
    parse_job() even after the job-identity fix (see
    test_pipeline.py's title-change identity test).
    """
    text = "عنوان المشروع : اختبار\n\nتفاصيل المشروع : وصف قصير\n"

    job = parse_job("NAFEZLY Channel", text)

    assert job["title"] == "اختبار"


def test_generic_message_falls_back_to_first_line_and_normalized_body():
    text = (
        "Power BI Dashboard Needed\n\n"
        "Need a dashboard built from sales data.\n"
        "Requirements include DAX measures and KPIs.\n\n"
        "Budget: $200\n"
        "https://mostaql.com/project/12345\n"
    )

    job = parse_job("Mostaql Jobs", text)

    assert job["title"] == "Power BI Dashboard Needed"
    assert job["url"] == "https://mostaql.com/project/12345"
    # Generic path has no explicit budget field extraction.
    assert job["budget"] == ""
    # Single newlines within a paragraph are collapsed to spaces.
    assert (
        "Need a dashboard built from sales data. "
        "Requirements include DAX measures and KPIs."
        in job["description"]
    )


def test_message_without_url_leaves_url_empty():
    job = parse_job("Generic Channel", "Just a title line\n\nSome description here.")

    assert job["title"] == "Just a title line"
    assert job["url"] == ""


def test_empty_source_does_not_crash_and_uses_generic_path():
    job = parse_job("", "Title only")

    assert job["title"] == "Title only"
    assert job["source"] == ""


def test_blank_lines_are_collapsed_to_a_single_paragraph_break():
    text = "Title\n\n\n\nParagraph one.\n\n\n\nParagraph two."

    job = parse_job("Generic Channel", text)

    # Three-or-more consecutive blank lines must not survive as
    # multiple blank lines in the normalized description.
    assert "\n\n\n" not in job["description"]
    assert "Paragraph one." in job["description"]
    assert "Paragraph two." in job["description"]
