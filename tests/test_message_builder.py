"""
app.message_builder tests. Pure string-building module, no app.config
dependency -- runs fully offline like test_keyword_filter.py.
"""

import re

import pytest

from app.message_builder import (
    MAX_MESSAGE_LENGTH,
    _safe_html_truncate,
    build_job_message,
)


def _assert_balanced_b_tags(html: str):
    assert html.count("<b>") == html.count("</b>"), (
        f"Unbalanced <b>/</b> tags in truncated message: {html!r}"
    )
    # No dangling/partial tag fragment left at all (an unterminated
    # "<...>" anywhere, not just at the very end).
    assert not re.search(r"<[^>]*$", html), (
        f"Dangling incomplete tag fragment found: {html!r}"
    )


def test_normal_short_message_is_not_truncated():
    message = build_job_message(
        title="Power BI Dashboard",
        description="Build a dashboard from sales data.",
        source="Test Channel",
        reason="Clear BI work.",
        budget="$200",
        categories=["Power BI", "Excel"],
    )

    assert len(message) <= MAX_MESSAGE_LENGTH
    assert "Power BI Dashboard" in message
    _assert_balanced_b_tags(message)


def test_long_description_is_truncated_and_stays_valid_html():
    message = build_job_message(
        title="Data Cleaning Job",
        description="A" * 10000,
        source="Test Channel",
        reason="Genuine analysis work.",
    )

    assert len(message) <= MAX_MESSAGE_LENGTH
    _assert_balanced_b_tags(message)


def test_long_llm_reason_is_truncated_and_stays_valid_html():
    message = build_job_message(
        title="Ambiguous Job",
        description="Short description.",
        source="Test Channel",
        reason="This is the LLM's reasoning. " * 500,
        ai_used=True,
    )

    assert len(message) <= MAX_MESSAGE_LENGTH
    _assert_balanced_b_tags(message)


def test_pathological_combination_still_produces_valid_html():
    """
    Long title, source, budget, and many categories together -- not
    just description/reason -- pushing the whole message over the
    limit even after the individual description/reason pre-truncation.
    """
    message = build_job_message(
        title="X" * 500,
        description="Y" * 500,
        source="Z" * 500,
        reason="W" * 500,
        budget="$" + "9" * 200,
        categories=[f"Category{i}" for i in range(100)],
        ai_used=True,
    )

    assert len(message) <= MAX_MESSAGE_LENGTH + 10  # small repair-tag headroom
    _assert_balanced_b_tags(message)


def test_channel_style_message_stays_valid_html_when_truncated():
    message = build_job_message(
        title="Long Channel Job " * 50,
        description="D" * 5000,
        source="Channel Source",
        reason="ignored in channel style",
        categories=[f"tag{i}" for i in range(50)],
        channel_style=True,
    )

    assert len(message) <= MAX_MESSAGE_LENGTH
    _assert_balanced_b_tags(message)



def test_channel_style_uses_final_category_name_in_header():
    message = build_job_message(
        title="FastAPI service",
        description="Build a REST API.",
        source="Test Channel",
        reason="Backend project.",
        category_name="Backend Development",
        channel_style=True,
    )

    assert "🚀 <b>New Backend Development Opportunity</b>" in message
    assert "Data Analysis Opportunity" not in message


def test_channel_style_escapes_category_name():
    message = build_job_message(
        title="Test Job",
        description="Test description.",
        source="Test Channel",
        reason="Test reason.",
        category_name="AI/ML <Special>",
        channel_style=True,
    )

    assert "AI/ML &lt;Special&gt;" in message


def test_channel_style_uses_abbreviated_arabic_source_names():
    """
    Public channel notifications should display abbreviated Arabic
    source names (مستقل, نفذلي, كفيل) instead of the full channel
    title or FreeHub source string.
    """
    test_cases = [
        ("Mostaql Jobs", "مستقل"),
        ("مستقل | برمجة، تطوير المواقع والتطبيقات", "مستقل"),
        ("NAFEZLY Channel", "نفذلي"),
        ("Nafezly - نفذلي", "نفذلي"),
        ("kafiil", "كفيل"),
        ("freelancer", "freelancer"),
    ]

    for source, expected_display in test_cases:
        message = build_job_message(
            title="Test Job",
            description="Test description.",
            source=source,
            reason="Test reason.",
            channel_style=True,
        )

        assert expected_display in message, (
            f"Expected '{expected_display}' in channel message for "
            f"source '{source}', got: {message!r}"
        )


def test_private_style_keeps_full_source_name():
    """
    Private chat notifications should keep the full original source
    name, not the abbreviated Arabic version.
    """
    message = build_job_message(
        title="Test Job",
        description="Test description.",
        source="Mostaql Jobs",
        reason="Test reason.",
        channel_style=False,
    )

    assert "Mostaql Jobs" in message
    assert "مستقل" not in message


# ------------------------------------------------------------------
# Direct unit tests of _safe_html_truncate against constructed inputs
# that land the cut exactly inside a tag -- the specific bug Fix 8
# targets. These pin the cut point precisely rather than relying on
# incidentally landing there via build_job_message's own content.
# ------------------------------------------------------------------


def test_safe_html_truncate_repairs_cut_landing_inside_open_tag():
    html = "prefix " + "x" * 50 + "<b>this text never gets closed because we cut here"
    cut_point = html.index("<b>") + 2  # lands mid "<b>" itself

    truncated = _safe_html_truncate(html, cut_point)

    _assert_balanced_b_tags(truncated)
    assert "<b>" not in truncated or truncated.count("<b>") == truncated.count("</b>")


def test_safe_html_truncate_repairs_cut_landing_inside_close_tag():
    html = "prefix <b>bold text</b" + "x" * 50  # closing tag deliberately incomplete
    cut_point = html.index("</b") + 2  # lands mid "</b>"

    truncated = _safe_html_truncate(html, cut_point)

    _assert_balanced_b_tags(truncated)


def test_safe_html_truncate_closes_an_open_span_left_dangling_by_the_cut():
    html = "before <b>never closed " + "z" * 200
    cut_point = 40  # well inside the open <b> span, past the tag itself

    truncated = _safe_html_truncate(html, cut_point)

    assert truncated.count("<b>") == 1
    assert truncated.count("</b>") == 1
    _assert_balanced_b_tags(truncated)


def test_safe_html_truncate_is_a_no_op_under_the_limit():
    html = "<b>short</b> message"
    assert _safe_html_truncate(html, 1000) == html


@pytest.mark.parametrize("cut_point", range(1, 40))
def test_safe_html_truncate_never_breaks_html_at_any_cut_point(cut_point):
    """
    Sweep the cut point across every position inside/around a <b> tag
    to make sure there's no boundary condition that still slips
    through unbalanced.
    """
    html = "start <b>bold span here</b> end " + "padding " * 50

    truncated = _safe_html_truncate(html, cut_point)

    _assert_balanced_b_tags(truncated)
