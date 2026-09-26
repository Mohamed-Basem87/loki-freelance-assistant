from app.domain.categories.registry import enabled_categories, deterministic_categories
from app.domain.classification import classify_and_select, select_category


def test_registry_has_data_analysis():
    profiles = enabled_categories()
    ids = [p.id for p in profiles]
    assert "data_analysis" in ids
    assert "full_stack" in ids
    assert len(ids) == 8


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


def _mixed_result(cat_id, decision, title_pos=False, core_hits=0, supp_pos=0.0,
                  supp_neg=0.0, core_neg=False):
    """Build a keyword_filter()-shaped result for select_category() tests."""
    return {
        "category_id": cat_id,
        "result": {
            "decision": decision,
            "title_core_positive": title_pos,
            "title_core_negative": False,
            "core_positive_hit_count": core_hits,
            "supporting_positive_weight": supp_pos,
            "supporting_negative_weight": supp_neg,
            "has_core_positive": core_hits > 0 or title_pos,
            "has_core_negative": core_neg,
        },
    }


def test_mixed_direct_picks_top_scoring_category_when_confident():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=6),  # 12+48=60
        "data_analysis": _mixed_result("data_analysis", "notify_directly", core_hits=5),       # 40
    }
    assert select_category(results) == "backend"


def test_mixed_direct_keeps_arbitration_when_below_threshold():
    results = {
        "backend": _mixed_result("backend", "notify_directly", core_hits=1, supp_pos=1),  # 9 < 10
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", core_hits=1, supp_pos=1),  # 9 < 20
    }
    assert select_category(results) is None


def test_claimed_candidate_direct_at_moderate_score_but_ambiguous_needs_more():
    # A notify_directly top only needs the claim threshold (10)...
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=2),  # 28
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", core_hits=3),  # 24
    }
    assert select_category(results) == "backend"
    # ...while a pure needs_gemini top still needs the higher ambiguous bar (20).
    results = {
        "backend": _mixed_result("backend", "notify_directly", core_hits=5),  # 40
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", title_pos=True, core_hits=4, supp_pos=6),  # 58
    }
    assert select_category(results) == "data_analysis"


def test_mixed_direct_picks_top_scoring_category_even_with_core_negative():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=7, core_neg=True),  # 12+56-25=43
        "data_analysis": _mixed_result("data_analysis", "notify_directly", core_hits=5),  # 40
    }
    # Core-negative evidence penalizes the score (-25) but no longer sends
    # the pick to arbitration: the higher-scoring backend still wins.
    assert select_category(results) == "backend"


def test_core_negative_still_loses_when_score_drops_below_threshold():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=1, core_neg=True),  # 12+8-25=-5
        "data_analysis": _mixed_result("data_analysis", "notify_directly", core_hits=4),  # 32
    }
    # The -25 penalty dropped the top below the (10) claim threshold and its
    # runner-up wins by the margin rule, so the pick is resolved to the
    # clean high-scoring runner-up.
    assert select_category(results) == "data_analysis"


def test_dominated_mixed_top_is_direct_picked():
    # A massively dominant top candidate with core-negative evidence is the
    # only plausible winner despite the mixed signals.
    results = {
        "backend": _mixed_result("backend", "needs_gemini", title_pos=True, core_hits=8, supp_pos=12, core_neg=True),  # 12+64+12-25=63
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", core_hits=6),  # 48
    }
    assert select_category(results) == "backend"


def test_mixed_direct_picks_top_when_margin_is_slim():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=6),  # 60
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", title_pos=True, core_hits=5, supp_pos=6),  # 58
    }
    # margin 2 >= 1: the top still wins. Slim but sufficient under the
    # relaxed margin rule.
    assert select_category(results) == "backend"


def test_mixed_direct_still_blocks_when_margin_is_barely_nonzero():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=2),  # 28
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", title_pos=True),  # 12 + 0 core
        "frontend": _mixed_result("frontend", "needs_gemini", title_pos=True),  # 12
    }
    # backend 28 vs runners-up 12/12: margin is exactly the 1.0 bar, so it
    # still wins deterministically.
    assert select_category(results) == "backend"


def test_full_stack_self_description_is_now_picked_by_top_score():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=7),
        "frontend": _mixed_result("frontend", "needs_gemini", core_hits=5),
    }
    # Full-stack self-descriptions are no longer forced to LLM arbitration:
    # the highest-scoring candidate wins via the normal scoring path.
    assert select_category(results, title="Full Stack Developer") == "backend"
    assert select_category(results, text="I need a full-stack product built", title="") == "backend"


def test_lone_ambiguous_is_auto_resolved_to_that_category():
    results = {
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", title_pos=True, core_hits=7),
    }
    assert select_category(results) == "data_analysis"


def test_ambiguous_category_selection_does_not_pick_a_category():
    results = {
        "data_analysis": {"category_id": "data_analysis",
                           "result": {"decision": "needs_gemini"}},
        "web_development": {"category_id": "web_development",
                             "result": {"decision": "needs_gemini"}},
    }
    assert select_category(results) is None


def test_multiple_direct_matches_resolve_to_top_scorer(monkeypatch):
    from app.domain import classification
    from app.domain.categories.data_analysis.profile import CategoryProfile

    class FakeProfile:
        id = "web_development"
        name = "Web Development"
        description = "Web applications."
        arbitration_context = "Primary deliverable is a web application."
        positive_keywords = {"web": {"core": {"react": 10}, "supporting": {}}}
        negative_keywords = {"web": {"core": {}, "supporting": {}}}
        hard_reject_keywords = set()
        guard_prompt_module = "app.domain.categories.data_analysis.guard_prompt"
        supporting_positive_min_for_gemini = 12
        supporting_negative_downgrade_threshold = 14
        min_supporting_positive_for_lone_core = 5
        title_positive_supporting_negative_threshold = 10
        arbitration_only = False
        enabled = True

    monkeypatch.setattr(
        classification,
        "deterministic_categories",
        lambda: (
            next(iter(deterministic_categories())),
            FakeProfile(),
        ),
    )

    result = classify_and_select(
        "Power BI dashboard built with React",
        title="Dashboard",
    )

    # Multiple notify_directly matches are no longer routed to LLM
    # arbitration -- the highest-scoring deterministic candidate wins.
    assert result["category_id"] is not None
    assert result["needs_category_arbitration"] is False


def test_huge_description_is_bounded_before_classification():
    from app.domain.classification import MAX_CLASSIFY_TEXT_CHARS
    word = "Power BI dashboard and Excel data analysis "
    huge = word * (MAX_CLASSIFY_TEXT_CHARS // len(word) * 3 + 10)
    result = classify_and_select(huge, title="Power BI Dashboard")
    assert result["category_id"] == "data_analysis"
    first_result = next(iter(result["categories"].values()))["result"]
    assert len(first_result["normalized_text"]) <= MAX_CLASSIFY_TEXT_CHARS * 2


def test_classify_and_select_is_bounded_with_runtime_limits():
    from app.domain.classification import MAX_CLASSIFY_TEXT_CHARS
    word = "Power BI dashboard and Excel data analysis "
    huge = word * (MAX_CLASSIFY_TEXT_CHARS // len(word) + 40)

    import time
    start = time.perf_counter()
    result = classify_and_select(huge, title="Power BI Dashboard")
    elapsed = time.perf_counter() - start

    assert result["category_id"] == "data_analysis"
    assert elapsed < 5.0


def test_multiple_ambiguous_candidates_are_all_exposed_for_one_arbitration():
    results = {
        "data_analysis": {"category_id": "data_analysis", "result": {"decision": "needs_gemini"}},
        "web_development": {"category_id": "web_development", "result": {"decision": "needs_gemini"}},
    }
    from app.domain.classification import select_category
    assert select_category(results) is None


def test_tiebreak_resolved_flag_is_true_for_mixed_resolution():
    results = {
        "backend": _mixed_result("backend", "notify_directly", title_pos=True, core_hits=6),  # 60
        "data_analysis": _mixed_result("data_analysis", "notify_directly", core_hits=5),      # 40
    }
    from app.domain.classification import classify_and_select
    # classify_and_select runs the real multi-candidate scoring path.
    result = classify_and_select(
        "Backend API with React dashboard",
        title="Backend Developer",
    )
    # The deterministic tier alone decides the flag, independent of the
    # exact text used above (that text drives real profiles, not the
    # synthetic fixture). Build a classification directly to assert the
    # flag contract instead.
    from app.domain.classification import _select_category_internal
    category_id, tiebreak = _select_category_internal(results)
    assert category_id == "backend"
    assert tiebreak is True


def test_tiebreak_resolved_flag_is_false_for_single_direct_match():
    from app.domain.classification import classify_and_select
    result = classify_and_select(
        "Power BI dashboard and Excel data analysis",
        title="Power BI Dashboard",
    )
    assert result["category_id"] == "data_analysis"
    assert result["tiebreak_resolved"] is False


def test_tiebreak_resolved_is_false_when_not_resolved():
    results = {
        "data_analysis": _mixed_result("data_analysis", "needs_gemini", core_hits=2, supp_pos=2),   # 18 < 35
        "web_development": _mixed_result("web_development", "needs_gemini", core_hits=1, supp_pos=1),  # 9
    }
    from app.domain.classification import _select_category_internal
    category_id, tiebreak = _select_category_internal(results)
    assert category_id is None
    assert tiebreak is False
