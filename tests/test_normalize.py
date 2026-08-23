from app.normalize import normalize


def test_normalize_inserts_space_between_latin_and_arabic_scripts():
    assert normalize("PowerBIلوحة") == "powerbi لوحه"
    assert normalize("لوحةPowerBI") == "لوحه powerbi"


def test_normalize_does_not_change_already_separated_scripts():
    assert normalize("Power BI لوحة") == "power bi لوحه"


def test_normalize_handles_mixed_script_in_longer_text():
    result = normalize("Build PowerBIلوحة with DAXلوحة")

    assert "powerbi لوحه" in result
    assert "dax لوحه" in result


def test_mixed_script_boundary_allows_keyword_match():
    from app.filters import keyword_filter
    from app.categories.data_analysis.profile import PROFILE

    result = keyword_filter("PowerBIلوحة", profile=PROFILE)

    assert result["matched"] is True
    assert any(
        hit["keyword"] == "powerbi"
        for hit in result["positive_core_matches"]
    )
