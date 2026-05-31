import pandas as pd

import app


def test_execute_text_to_sql_repairs_once(monkeypatch):
    calls = {"run": 0, "repair": 0}

    def fake_run_query(sql):
        calls["run"] += 1
        if calls["run"] == 1:
            raise ValueError("bad column")
        return pd.DataFrame({"region": ["South"], "revenue": [1067.1]})

    def fake_repair_sql(question, bad_sql, error):
        calls["repair"] += 1
        return {"sql": "SELECT 'South' AS region, 1067.1 AS revenue", "explanation": "fixed"}

    monkeypatch.setattr(app, "run_query", fake_run_query)
    monkeypatch.setattr(app, "repair_sql", fake_repair_sql)
    monkeypatch.setattr(app, "self_check_sql", lambda question, sql, df: {"passes": True, "reason": "ok"})

    result = app.execute_text_to_sql("least region", "SELECT bad_column FROM orders", "test")
    assert result["repaired"] is True
    assert calls["repair"] == 1
    assert result["self_check"]["passes"] is True


def test_self_check_payload_can_be_mocked(monkeypatch):
    monkeypatch.setattr(app, "get_openai_client", lambda: None)
    result = app.self_check_sql("question", "SELECT 1", pd.DataFrame({"x": [1]}))
    assert result["passes"] is True
    assert "skipped" in result["reason"]
