from app.categories.registry import enabled_categories
from app.classification import classify_and_select, select_category


def test_registry_has_data_analysis():
    profiles = enabled_categories()
    assert [p.id for p in profiles] == ["data_analysis"]


def test_direct_match_selects_one_final_category():
    result = classify_and_select(
        "Power BI dashboard and Excel data analysis",
        title="Power BI Dashboard",
    )
    assert result["category_id"] == "data_analysis"
    assert result["has_direct_match"] is True
    assert result["needs_category_arbitration"] is False


def test_direct_plus_ambiguous_does_not_pick_a_category():
    results = {
        "data_analysis": {"category_id": "data_analysis",
                           "result": {"decision": "notify_directly"}},
        "web_development": {"category_id": "web_development",
                            "result": {"decision": "needs_gemini"}},
    }
    assert select_category(results) is None


def test_ambiguous_category_selection_does_not_pick_a_category():
    results = {
        "data_analysis": {"category_id": "data_analysis",
                           "result": {"decision": "needs_gemini"}},
        "web_development": {"category_id": "web_development",
                             "result": {"decision": "needs_gemini"}},
    }
    assert select_category(results) is None
