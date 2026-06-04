import app


def test_retrieve_schema_context_for_product_units(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    context = app.retrieve_schema_context("Which product sold the most units?")

    assert "products(product_id" in context
    assert "orders(order_id" in context
    assert "Units sold formula" in context


def test_retrieve_schema_context_for_customer_segments(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    context = app.retrieve_schema_context("Show revenue by customer segment.")

    assert "customers(customer_id" in context
    assert "segment" in context
    assert "orders.customer_id = customers.customer_id" in context


def test_retrieve_schema_context_falls_back_to_tables(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    context = app.retrieve_schema_context("What should I inspect next?")

    assert "Retrieved schema context" in context
    assert "customers(customer_id" in context
    assert "orders.product_id = products.product_id" in context
