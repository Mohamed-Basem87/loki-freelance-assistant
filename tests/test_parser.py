"""
app.services.parser tests. No app.infra.config dependency, so this runs fully offline
regardless of credentials -- same as test_keyword_filter.py.

The previous version of this file only pprint()'d one Nafezly example
for manual inspection, with no assertions at all, so a parsing
regression here would never fail a test run. This keeps that same
example (as a real assertion) and adds the generic/Mostaql fallback
path and a couple of edge cases the parser is supposed to handle.
"""

from app.services.parser import parse_job


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
    string -- see app.services.parser.parse_job's `source_name = (source or
    "").lower()` check. This is a real, load-bearing use of the
    channel/source string (not just display metadata), which is why
    app.services.message_processor must keep passing the channel title into
    parse_job() even after the job-identity fix (see
    test_pipeline.py's title-change identity test).
    """
    text = "عنوان المشروع : اختبار\n\nتفاصيل المشروع : وصف قصير\n"

    job = parse_job("NAFEZLY Channel", text)

    assert job["title"] == "اختبار"


def test_nafezly_detection_by_arabic_name():
    """
    The Nafezly-specific parsing path also triggers when the source
    string contains the Arabic name "نفذلي" -- this handles the case
    where the source is already the abbreviated display name (e.g.
    from FreeHub or a previously-normalized channel title).
    """
    text = "عنوان المشروع : اختبار عربي\n\nتفاصيل المشروع : وصف قصير\n"

    job = parse_job("نفذلي", text)

    assert job["title"] == "اختبار عربي"


def test_mostaql_profile_badge_is_removed_before_title_and_description():
    text = (
        "صانع محتوى\n\n"
        "Power BI Dashboard Needed\n\n"
        "Need a dashboard built from sales data.\n"
        "DAX and KPI reporting required.\n"
    )

    job = parse_job("Mostaql Jobs", text)

    assert job["title"] == "Power BI Dashboard Needed"
    assert "صانع محتوى" not in job["description"]
    assert "Need a dashboard built from sales data." in job["description"]


def test_mostaql_badge_is_not_stripped_when_it_is_the_only_line():
    job = parse_job("Mostaql Jobs", "صانع محتوى")

    assert job["title"] == "صانع محتوى"
    assert job["description"] == ""


def test_mostaql_stats_header_badge_line_is_removed():
    """
    Stats-header channel layout: the whole 👤/💼/💵 metadata block
    sits between the title and the description marker, so the
    first-line-only strip used to miss it and profession labels
    (e.g. معلم) leaked into the classifier input, firing education
    core-negatives on genuine platform builds.
    """
    text = (
        "🎯 تطوير منصة تدريب واختبارات\n"
        "\u200f👤 A L I A.\n"
        "\u200f💼 معلم\n"
        "\u200f💵 $2500.00 - $5000.00\n"
        "\u200f⌛ 30 يوما\n"
        "\u200f📊 0.0%\n"
        "~~~~ الوصف ~~~~\n"
        "أبحث عن مبرمج لتطوير منصة تعليمية.\n"
    )

    job = parse_job("مستقل | برمجة، تطوير المواقع والتطبيقات", text)

    assert job["title"] == "🎯 تطوير منصة تدريب واختبارات"
    assert "معلم" not in job["description"]
    # The whole stats block is metadata and is stripped with it.
    assert "$2500.00" not in job["description"]
    assert "A L I A." not in job["description"]
    # The 💵 line is structured data: captured into budget instead of
    # being lost with the rest of the header block.
    assert job["budget"] == "$2500.00 - $5000.00"
    # The الوصف marker line itself is stripped too, so it never leaks
    # into the notified description.
    assert "الوصف" not in job["description"]
    # Description prose after the marker survives untouched.
    assert "أبحث عن مبرمج لتطوير منصة تعليمية." in job["description"]


def test_profession_word_inside_description_body_is_kept():
    """
    The header-region strip must never touch description prose: a
    line after the الوصف marker that merely says a profession word
    stays part of the description.
    """
    text = (
        "🎯 Build me a dashboard\n"
        "\u200f👤 Client X.\n"
        "\u200f💼 مدخل بيانات\n"
        "\u200f💵 $50 - $100\n"
        "~~~~ الوصف ~~~~\n"
        "المطلوب مطور\n"
        "للوحة تحكم بسيطة.\n"
    )

    job = parse_job("مستقل | برمجة، تطوير المواقع والتطبيقات", text)

    assert "المطلوب مطور للوحة تحكم بسيطة." in job["description"]


def test_mostaql_markerless_profile_header_is_stripped():
    """
    Current channel layout: no الوصف marker. The title is followed by
    the 👤/💼/💵/⌛/✍️/📂 profile block, then the description. The
    whole block is metadata and must not reach the description (the
    client profession used to contaminate classification, e.g. a
    teacher-client's "معلم" on a store-build job).
    """
    text = (
        "\u200f💼 إنشاء متجر إلكتروني لبيع العسل\n"
        "\u200f👤 Reda T.\n"
        "\u200f💼 معلم\n"
        "\u200f💵 $100.00 - $250.00\n"
        "\u200f⌛ 5 أيام\n"
        "\u200f✍️ مسجل منذ: 22 أكتوبر 2026\n"
        "\u200f📂 المشاريع المفتوحة: 2\n"
        "\u200fمطلوب إنشاء متجر إلكتروني عربي لمنتجات العسل.\n"
        "\u200f🧩 #برمجة_مواقع #إنشاء_متجر_إلكتروني\n"
    )

    job = parse_job("مستقل | برمجة، تطوير المواقع والتطبيقات", text)

    # Title behaviour is unchanged: the channel's RLM + 💼 prefix stays.
    assert job["title"] == "\u200f💼 إنشاء متجر إلكتروني لبيع العسل"
    for leaked in (
        "Reda T.", "👤", "💼", "$100.00", "⌛", "مسجل منذ",
        "المشاريع المفتوحة", "⌛ 5 أيام",
    ):
        assert leaked not in job["description"]
    assert "مطلوب إنشاء متجر إلكتروني عربي لمنتجات العسل." in job["description"]
    assert "#برمجة_مواقع" in job["description"]


def test_mostaql_older_header_with_details_divider_after_blank_is_stripped():
    """
    Older variant of the same leak: "✍️ تاريخ التسجيل" + "📊 56.2%"
    labels, then a blank line, then a "📄 تفاصيل المشروع:" divider and
    another blank before the body. The divider is metadata, not prose.
    """
    text = (
        "\u200fتعديل الأسعار والضريبة في أودو\n"
        "\u200f👤 صاحب المشروع: The T.\n"
        "\u200f💵 $250.00 - $500.00\n"
        "\u200f⌛ 10 أيام\n"
        "\u200f✍️ تاريخ التسجيل: 04 مارس 2020\n"
        "\u200f📊 معدل التوظيف: 56.2%\n"
        "\n"
        "\u200f📄 تفاصيل المشروع:\n"
        "\n"
        "\u200fتعديل الاسعار والضريبة في odoo حسب المنيو\n"
    )

    job = parse_job("مستقل | دعم، مساعدة وإدخال بيانات", text)

    assert job["title"] == "\u200fتعديل الأسعار والضريبة في أودو"
    for leaked in (
        "صاحب المشروع", "The T.", "$250.00", "⌛", "تاريخ التسجيل",
        "معدل التوظيف", "📄 تفاصيل المشروع",
    ):
        assert leaked not in job["description"]
    assert "تعديل الاسعار والضريبة في odoo حسب المنيو" in job["description"]


def test_mostaql_markerless_body_heading_after_blank_is_kept():
    """
    Only the contiguous block right after the title is metadata. A body
    section heading that happens to start with 💼, separated from the
    header by a blank line, must survive.
    """
    text = (
        "\u200fتخصيص نظام أودو لمركز خدمي\n"
        "\u200f👤 Mahmoud A.\n"
        "\u200f💼 ادارة أعمال\n"
        "\u200f💵 $250.00 - $500.00\n"
        "\n"
        "💼 المؤهلات والشروط المطلوبة\n"
        "خبرة لا تقل عن 3 سنوات في Odoo.\n"
    )

    job = parse_job("مستقل | برمجة، تطوير المواقع والتطبيقات", text)

    assert "ادارة أعمال" not in job["description"]
    assert "Mahmoud A." not in job["description"]
    assert "💼 المؤهلات والشروط المطلوبة" in job["description"]
    assert "خبرة لا تقل عن 3 سنوات في Odoo." in job["description"]


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
    assert job["title"] not in job["description"]


def test_extracted_url_trims_trailing_prose_punctuation():
    """
    P3 regression test: the URL regex's \\S+ is greedy and used to
    sweep trailing prose punctuation (a sentence-ending period, a
    comma before the next clause) into the "Open Project" button's
    target URL.
    """
    text = "Check this out: https://mostaql.com/project/999. Great fit!"

    job = parse_job("Generic Channel", text)

    assert job["url"] == "https://mostaql.com/project/999"


def test_extracted_url_strips_wrapping_paren_not_part_of_url():
    text = "See the brief (https://mostaql.com/project/999) for details."

    job = parse_job("Generic Channel", text)

    # The opening "(" is prose, not part of the URL, so it's never
    # part of the regex match in the first place -- the trailing ")"
    # is correctly stripped as trailing punctuation.
    assert job["url"] == "https://mostaql.com/project/999"


def test_extracted_url_keeps_paren_that_is_actually_part_of_the_url():
    text = (
        "Reference: https://en.wikipedia.org/wiki/Freelancer_(disambiguation) "
        "for background."
    )

    job = parse_job("Generic Channel", text)

    assert job["url"] == (
        "https://en.wikipedia.org/wiki/Freelancer_(disambiguation)"
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


def test_parser_source_alias_does_not_match_as_substring():
    from app.adapters.parsers.registry import ParserAdapter
    adapter = ParserAdapter("nafezly", ("nafezly", "نفذلي"), lambda *_: {})
    assert adapter.matches("Nafezly")
    assert not adapter.matches("notnafezlytoken")
