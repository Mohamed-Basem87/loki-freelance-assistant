from app.categories.registry import enabled_categories
from app.classification import classify_and_select, select_category


def test_registry_has_data_analysis():
    profiles = enabled_categories()
    ids = [p.id for p in profiles]
    assert "data_analysis" in ids
    assert len(ids) == 6


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


def test_multiple_direct_matches_require_arbitration(monkeypatch):
    from app import classification
    from app.categories.data_analysis.profile import CategoryProfile

    class FakeProfile:
        id = "web_development"
        name = "Web Development"
        description = "Web applications."
        arbitration_context = "Primary deliverable is a web application."
        positive_keywords = {"web": {"core": {"react": 10}, "supporting": {}}}
        negative_keywords = {"web": {"core": {}, "supporting": {}}}
        hard_reject_keywords = set()
        guard_prompt_module = "app.categories.data_analysis.guard_prompt"
        supporting_positive_min_for_gemini = 12
        supporting_negative_downgrade_threshold = 14
        min_supporting_positive_for_lone_core = 5
        title_positive_supporting_negative_threshold = 10

    monkeypatch.setattr(
        classification,
        "enabled_categories",
        lambda: (
            next(iter(enabled_categories())),
            FakeProfile(),
        ),
    )

    result = classify_and_select(
        "Power BI dashboard built with React",
        title="Dashboard",
    )

    assert result["category_id"] is None
    assert result["needs_category_arbitration"] is True


def test_multiple_ambiguous_candidates_are_all_exposed_for_one_arbitration():
    results = {
        "data_analysis": {"category_id": "data_analysis", "result": {"decision": "needs_gemini"}},
        "web_development": {"category_id": "web_development", "result": {"decision": "needs_gemini"}},
    }
    from app.classification import select_category
    assert select_category(results) is None
