"""Provider-error paths must not echo pasted merchant/customer content into logs."""

import logging

import pytest

from app.extractor import ExtractionUnavailable, GeminiExtractor, _parse_json_response


def test_invalid_model_output_error_does_not_include_response_content():
    sensitive = "Customer phone 01001234567 and address Cairo"
    with pytest.raises(ValueError) as error:
        _parse_json_response(f"not-json: {sensitive}")
    assert sensitive not in str(error.value)
    assert "invalid structured output" in str(error.value)


def test_failed_extraction_logs_only_error_type(caplog, monkeypatch):
    sensitive = "Name Mohamed; phone 01001234567"
    extractor = object.__new__(GeminiExtractor)
    extractor.candidate_models = ["test-model"]
    extractor.max_attempts = 1
    extractor.deadline_seconds = 5.0

    def invalid_response(_model, _prompt):
        return f"not-json: {sensitive}"

    monkeypatch.setattr(extractor, "_generate", invalid_response)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ExtractionUnavailable) as error:
            extractor.extract("synthetic order")

    assert sensitive not in str(error.value)
    assert sensitive not in caplog.text
    assert "error_type=ValueError" in caplog.text
