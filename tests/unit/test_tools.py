import pandas as pd

import app


def test_infer_sort_order_least():
    assert app.infer_sort_order("Which region has the least revenue?") == "asc"


def test_infer_sort_order_most():
    assert app.infer_sort_order("Which product has the most revenue?") == "desc"


def test_sql_for_question_ascending_revenue():
    sql = app.sql_for_question("Revenue by region", "asc")
    assert "ORDER BY revenue ASC" in sql


def test_add_converted_revenue_adds_currency_column():
    df = pd.DataFrame({"region": ["West"], "revenue": [100.0]})
    converted = app.add_converted_revenue(df, "CAD", 1.38)
    assert converted.loc[0, "revenue_cad"] == 138.0


def test_format_sql_breaks_major_clauses():
    sql = app.format_sql("SELECT a, b FROM t JOIN u ON t.id = u.id GROUP BY a ORDER BY b DESC")
    assert "\nFROM" in sql
    assert "\nJOIN" in sql
    assert "\nGROUP BY" in sql
    assert "\nORDER BY" in sql
