import json

import pytest

from app.llm.utils import parse_response


VALID = {
    "decision": "accept",
    "confidence": 90,
    "project_type": "Business Intelligence",
    "primary_deliverable": "Power BI dashboard",
    "reason": "The project centers on analytical dashboard work.",
    "skills_detected": ["Power BI", "DAX"],
}


def test_parse_response_accepts_valid_schema():
    assert parse_response(json.dumps(VALID)) == VALID


@pytest.mark.parametrize("decision", ["banana", "", 1, None])
def test_parse_response_rejects_invalid_decision(decision):
    with pytest.raises(ValueError, match="Invalid LLM decision"):
        parse_response(json.dumps({**VALID, "decision": decision}))


@pytest.mark.parametrize("confidence", [-1, 101, "90", None, True, False])
def test_parse_response_rejects_invalid_confidence(confidence):
    with pytest.raises(ValueError, match="Invalid LLM confidence"):
        parse_response(json.dumps({**VALID, "confidence": confidence}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_type", 123),
        ("primary_deliverable", None),
        ("reason", ["not", "a", "string"]),
        ("skills_detected", "Power BI"),
        ("skills_detected", [1, "DAX"]),
    ],
)
def test_parse_response_rejects_invalid_field_types(field, value):
    with pytest.raises(ValueError, match="Invalid LLM"):
        parse_response(json.dumps({**VALID, field: value}))


def test_parse_response_rejects_non_object_json():
    with pytest.raises(ValueError, match="JSON object"):
        parse_response(json.dumps(["accept", 90]))


def test_parse_response_rejects_malformed_json():
    with pytest.raises(json.JSONDecodeError):
        parse_response("{not valid json}")


def test_parse_response_accepts_fenced_json():
    assert parse_response(f"```json\n{json.dumps(VALID)}\n```") == VALID
