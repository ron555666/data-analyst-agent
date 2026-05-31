import json

import pytest

import app


def test_validate_sql_allows_select():
    ok, message = app.validate_sql("SELECT * FROM orders")
    assert ok
    assert message == ""


def test_validate_sql_blocks_drop():
    ok, message = app.validate_sql("DROP TABLE orders")
    assert not ok
    assert "only SELECT" in message


def test_validate_sql_blocks_multiple_statements():
    ok, message = app.validate_sql("SELECT * FROM orders; SELECT * FROM customers")
    assert not ok
    assert "multiple statements" in message


def test_external_api_allowlist_blocks_unknown_host():
    with pytest.raises(ValueError, match="not allowed"):
        app.validate_external_api_url("https://example.com/rates")


def test_audit_event_writes_jsonl(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(app, "AUDIT_LOG_PATH", audit_path)
    app.audit_event("tool_call", "unit_test", "success", {"ok": True})
    event = json.loads(audit_path.read_text(encoding="utf-8"))
    assert event["tool"] == "unit_test"
    assert event["details"]["ok"] is True
