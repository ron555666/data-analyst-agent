import json
import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

import matplotlib.pyplot as plt
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

try:
    from mem0 import Memory as Mem0Memory
except ImportError:
    Mem0Memory = None

load_dotenv()


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sales_data.db"
MEMORY_PATH = BASE_DIR / "memory.json"
AUDIT_LOG_PATH = BASE_DIR / "audit_events.jsonl"
MEMORY_USER_ID = "data_analyst_demo_user"
DEFAULT_BASE_CURRENCY = "USD"
SUPPORTED_CURRENCIES = ["USD", "CAD", "CNY", "EUR", "GBP", "JPY"]
ALLOWED_API_HOSTS = {"api.frankfurter.dev"}
DEFAULT_USER_QUESTION = "Which region generated the most revenue?"

DANGEROUS_WORDS = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "grant",
    "revoke",
}

SUGGESTED_QUESTIONS = {
    "Revenue by region": """
        SELECT c.region,
               ROUND(SUM(o.quantity * p.unit_price * (1 - o.discount)), 2) AS revenue
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        JOIN products p ON o.product_id = p.product_id
        GROUP BY c.region
        ORDER BY revenue DESC
    """,
    "Top products": """
        SELECT p.product_name,
               p.category,
               SUM(o.quantity) AS units_sold,
               ROUND(SUM(o.quantity * p.unit_price * (1 - o.discount)), 2) AS revenue
        FROM orders o
        JOIN products p ON o.product_id = p.product_id
        GROUP BY p.product_id
        ORDER BY revenue DESC
    """,
    "Monthly revenue": """
        SELECT substr(o.order_date, 1, 7) AS month,
               ROUND(SUM(o.quantity * p.unit_price * (1 - o.discount)), 2) AS revenue
        FROM orders o
        JOIN products p ON o.product_id = p.product_id
        GROUP BY month
        ORDER BY month
    """,
    "Customer segments": """
        SELECT c.segment,
               COUNT(DISTINCT c.customer_id) AS customers,
               ROUND(SUM(o.quantity * p.unit_price * (1 - o.discount)), 2) AS revenue
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        JOIN products p ON o.product_id = p.product_id
        GROUP BY c.segment
        ORDER BY revenue DESC
    """,
}


@st.cache_resource(show_spinner=False)
def get_openai_client():
    if not os.getenv("OPENAI_API_KEY"):
        return None
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def audit_event(event_type: str, tool: str, status: str, details: dict | None = None) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "tool": tool,
        "status": status,
        "details": details or {},
    }
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(event) + "\n")


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {
            "chart_type": "bar",
            "preferred_currency": "CAD",
            "last_question": "",
            "analyses_run": 0,
        }

    memory = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    memory.setdefault("chart_type", "bar")
    memory.setdefault("preferred_currency", "CAD")
    memory.setdefault("last_question", "")
    memory.setdefault("analyses_run", 0)
    return memory


def save_memory(memory: dict) -> None:
    MEMORY_PATH.write_text(json.dumps(memory, indent=2), encoding="utf-8")


def route_question_with_llm(question: str) -> dict:
    client = get_openai_client()
    available_tools = list(SUGGESTED_QUESTIONS)

    if client is None:
        fallback_tool = route_question_with_rules(question)
        audit_event(
            "tool_call",
            "llm_router",
            "fallback",
            {"reason": "OPENAI_API_KEY unavailable", "question": question, "routed_tool": fallback_tool},
        )
        return {
            "tool": fallback_tool,
            "reason": "OpenAI was unavailable, so the app used keyword routing.",
        }

    prompt = {
        "role": "system",
        "content": (
            "You are a safe routing layer for a Data Analyst Agent. "
            "Choose exactly one approved analysis tool for the user's question. "
            "Do not generate SQL. Do not invent tools. "
            f"Approved tools: {', '.join(available_tools)}. "
            "Return only JSON with keys: tool, reason."
        ),
    }
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[prompt, {"role": "user", "content": question}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    payload = json.loads(response.choices[0].message.content)
    tool = payload.get("tool")
    if tool not in SUGGESTED_QUESTIONS:
        audit_event(
            "security_block",
            "llm_router",
            "blocked",
            {"reason": "LLM returned a non-allowlisted tool.", "question": question, "raw_tool": tool},
        )
        tool = route_question_with_rules(question)
        payload["reason"] = "The LLM returned an invalid tool, so the app used safe fallback routing."

    audit_event(
        "tool_call",
        "llm_router",
        "success",
        {"question": question, "routed_tool": tool, "reason": payload.get("reason", "")},
    )
    return {"tool": tool, "reason": payload.get("reason", "")}


def route_question_with_rules(question: str) -> str:
    text = question.lower()
    if any(word in text for word in ["month", "monthly", "trend", "over time", "date"]):
        return "Monthly revenue"
    if any(word in text for word in ["product", "products", "item", "items"]):
        return "Top products"
    if any(word in text for word in ["segment", "customer type", "enterprise", "retail"]):
        return "Customer segments"
    return "Revenue by region"


@st.cache_data(show_spinner=False)
def route_question(question: str) -> dict:
    return route_question_with_llm(question)


@st.cache_resource(show_spinner=False)
def get_mem0_memory():
    if Mem0Memory is None or not os.getenv("OPENAI_API_KEY"):
        return None

    config = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-4o-mini",
                "temperature": 0.1,
                "max_tokens": 1000,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "text-embedding-3-small",
                "embedding_dims": 1536,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "data_analyst_agent_memories",
                "path": str(BASE_DIR / ".mem0_qdrant"),
                "embedding_model_dims": 1536,
                "on_disk": True,
            },
        },
        "history_db_path": str(BASE_DIR / ".mem0_history.db"),
    }
    return Mem0Memory.from_config(config)


def mem0_status() -> str:
    if Mem0Memory is None:
        return "Mem0 unavailable: `mem0ai` is not installed."
    if not os.getenv("OPENAI_API_KEY"):
        return "Mem0 unavailable: set `OPENAI_API_KEY` first."
    return "Mem0 enabled with local JSON fallback."


def add_mem0_memory(content: str) -> bool:
    mem0 = get_mem0_memory()
    if mem0 is None:
        audit_event(
            "tool_call",
            "mem0_memory",
            "fallback",
            {"reason": "Mem0 unavailable; using local JSON fallback."},
        )
        return False

    messages = [
        {"role": "user", "content": content},
        {"role": "assistant", "content": "I will remember this for future analysis sessions."},
    ]
    mem0.add(messages, user_id=MEMORY_USER_ID)
    audit_event("tool_call", "mem0_memory", "success", {"user_id": MEMORY_USER_ID})
    return True


def search_mem0_memories(query: str) -> list[str]:
    mem0 = get_mem0_memory()
    if mem0 is None:
        return []

    try:
        raw_results = mem0.search(query, filters={"user_id": MEMORY_USER_ID})
    except TypeError:
        raw_results = mem0.search(query, user_id=MEMORY_USER_ID)

    if isinstance(raw_results, dict):
        raw_results = raw_results.get("results", [])

    memories = []
    for item in raw_results[:5]:
        if isinstance(item, dict):
            memories.append(str(item.get("memory") or item.get("text") or item))
        else:
            memories.append(str(item))
    return memories


def remember_analysis_context(memory: dict, question: str) -> bool:
    content = (
        f"User prefers {memory['preferred_currency']} currency and "
        f"{memory['chart_type']} charts. Last data analysis request: {question}."
    )
    return add_mem0_memory(content)


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).rstrip(";")


def validate_sql(sql: str) -> tuple[bool, str]:
    cleaned = normalize_sql(sql).lower()
    tokens = set(re.findall(r"[a-z_]+", cleaned))

    if not cleaned.startswith("select "):
        message = "Security rule: only SELECT queries are allowed."
        audit_event("security_block", "query_database", "blocked", {"reason": message, "sql": sql})
        return False, message
    if ";" in cleaned:
        message = "Security rule: multiple statements are not allowed."
        audit_event("security_block", "query_database", "blocked", {"reason": message, "sql": sql})
        return False, message
    blocked = sorted(tokens.intersection(DANGEROUS_WORDS))
    if blocked:
        message = f"Security rule: blocked keyword(s): {', '.join(blocked)}."
        audit_event(
            "security_block",
            "query_database",
            "blocked",
            {"reason": message, "blocked_keywords": blocked, "sql": sql},
        )
        return False, message
    return True, ""


def run_query(sql: str) -> pd.DataFrame:
    ok, message = validate_sql(sql)
    if not ok:
        raise ValueError(message)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        result = pd.read_sql_query(normalize_sql(sql), conn)
        audit_event(
            "tool_call",
            "query_database",
            "success",
            {"rows_returned": len(result), "sql": normalize_sql(sql)},
        )
        return result
    finally:
        conn.close()


def validate_external_api_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        message = "Security rule: external API calls must use HTTPS."
        audit_event("security_block", "external_api", "blocked", {"reason": message, "url": url})
        raise ValueError(message)
    if parsed.netloc not in ALLOWED_API_HOSTS:
        message = f"Security rule: external API host is not allowed: {parsed.netloc}"
        audit_event(
            "security_block",
            "external_api",
            "blocked",
            {"reason": message, "url": url, "host": parsed.netloc},
        )
        raise ValueError(message)


def fetch_exchange_rate(base: str, target: str) -> float:
    base = base.upper()
    target = target.upper()

    if base not in SUPPORTED_CURRENCIES or target not in SUPPORTED_CURRENCIES:
        message = "Security rule: unsupported currency requested."
        audit_event(
            "security_block",
            "fetch_exchange_rate",
            "blocked",
            {"reason": message, "base": base, "target": target},
        )
        raise ValueError(message)
    if base == target:
        audit_event(
            "tool_call",
            "fetch_exchange_rate",
            "success",
            {"base": base, "target": target, "rate": 1.0, "source": "identity"},
        )
        return 1.0

    url = f"https://api.frankfurter.dev/v2/rates?base={base}&quotes={target}"
    validate_external_api_url(url)

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list) and data:
        rate = float(data[0]["rate"])
    else:
        rate = float(data["rates"][target])
    audit_event(
        "tool_call",
        "fetch_exchange_rate",
        "success",
        {"base": base, "target": target, "rate": rate, "host": "api.frankfurter.dev"},
    )
    return rate


def add_converted_revenue(df: pd.DataFrame, target_currency: str, rate: float) -> pd.DataFrame:
    converted = df.copy()
    if target_currency == DEFAULT_BASE_CURRENCY:
        return converted

    revenue_cols = [
        col for col in converted.select_dtypes(include="number").columns if "revenue" in col.lower()
    ]
    for col in revenue_cols:
        converted[f"{col}_{target_currency.lower()}"] = (converted[col] * rate).round(2)
    audit_event(
        "tool_call",
        "add_converted_revenue",
        "success",
        {"target_currency": target_currency, "rate": rate, "converted_columns": revenue_cols},
    )
    return converted


def read_recent_audit_events(limit: int = 8) -> list[dict]:
    if not AUDIT_LOG_PATH.exists():
        return []
    lines = AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]


def summarize(df: pd.DataFrame) -> str:
    if df.empty:
        return "No rows matched this analysis."

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if not numeric_cols:
        return f"Returned {len(df)} rows. Review the table for categorical patterns."

    metric = numeric_cols[-1]
    top_row = df.sort_values(metric, ascending=False).iloc[0]
    label_cols = [col for col in df.columns if col != metric]
    label = " / ".join(str(top_row[col]) for col in label_cols[:2])
    total = df[metric].sum()
    return f"Returned {len(df)} rows. Highest {metric}: {label} ({top_row[metric]:,.2f}). Total {metric}: {total:,.2f}."


def plot_result(df: pd.DataFrame, chart_type: str) -> None:
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if df.empty or not numeric_cols or len(df.columns) < 2:
        return

    metric = numeric_cols[-1]
    label_col = next((col for col in df.columns if col != metric), df.columns[0])
    fig, ax = plt.subplots(figsize=(8, 4.5))

    if chart_type == "line":
        ax.plot(df[label_col].astype(str), df[metric], marker="o")
    else:
        ax.bar(df[label_col].astype(str), df[metric])

    ax.set_xlabel(label_col)
    ax.set_ylabel(metric)
    ax.tick_params(axis="x", rotation=30)
    fig.tight_layout()
    st.pyplot(fig)


def ensure_database() -> None:
    if DB_PATH.exists():
        return
    st.warning("sales_data.db was not found. Run `python create_db.py` first.")
    st.stop()


def main() -> None:
    st.set_page_config(page_title="Data Analyst Agent", layout="wide")
    ensure_database()

    memory = load_memory()

    st.title("Data Analyst Agent")
    st.caption("Capstone components: Tools, Memory, and Security")

    with st.sidebar:
        st.header("Memory")
        st.caption(mem0_status())
        chart_type = st.radio(
            "Preferred chart",
            ["bar", "line"],
            index=["bar", "line"].index(memory.get("chart_type", "bar")),
        )
        preferred_currency = st.selectbox(
            "Preferred currency",
            SUPPORTED_CURRENCIES,
            index=SUPPORTED_CURRENCIES.index(memory.get("preferred_currency", "CAD")),
        )
        memory["chart_type"] = chart_type
        memory["preferred_currency"] = preferred_currency
        save_memory(memory)

        st.metric("Analyses run", memory.get("analyses_run", 0))
        if memory.get("last_question"):
            st.write("Last question")
            st.code(memory["last_question"])
        memories = search_mem0_memories("What are the user's data analysis preferences?")
        if memories:
            with st.expander("Mem0 retrieved memories"):
                for item in memories:
                    st.write(item)

        st.header("Security")
        st.write("Only read-only SELECT queries are accepted.")
        st.write("Dangerous SQL keywords and multi-statement queries are blocked.")
        st.write("External API calls are limited to `api.frankfurter.dev`.")
        with st.expander("Recent audit events"):
            events = read_recent_audit_events()
            if events:
                st.json(events)
            else:
                st.write("No audit events yet.")

    user_question = st.text_input("Ask a business question", value=DEFAULT_USER_QUESTION)
    manual_mode = st.checkbox("Choose analysis tool manually")

    if manual_mode:
        question = st.selectbox("Choose an analysis tool", list(SUGGESTED_QUESTIONS))
        route_reason = "Manual tool selection."
    else:
        route = route_question(user_question)
        question = route["tool"]
        route_reason = route["reason"]
        st.info(f"LLM routed this question to: {question}")

    sql = st.text_area("SQL generated by the agent", SUGGESTED_QUESTIONS[question], height=180)

    if st.button("Run analysis", type="primary"):
        try:
            result = run_query(sql)
            exchange_rate = fetch_exchange_rate(DEFAULT_BASE_CURRENCY, memory["preferred_currency"])
            result = add_converted_revenue(result, memory["preferred_currency"], exchange_rate)
            memory["last_question"] = question
            memory["analyses_run"] = memory.get("analyses_run", 0) + 1
            save_memory(memory)
            mem0_saved = remember_analysis_context(memory, question)

            st.subheader("Answer")
            st.success(summarize(result))
            with st.expander("Tool call trace", expanded=True):
                st.code(
                    "\n".join(
                        [
                            f"User question: {user_question}",
                            f"Tool 0: llm_router -> {question}",
                            f"Routing reason: {route_reason}",
                            "Tool 1: query_database(sql)",
                            f"Tool 2: fetch_exchange_rate(base='{DEFAULT_BASE_CURRENCY}', target='{memory['preferred_currency']}')",
                            f"Exchange rate: 1 {DEFAULT_BASE_CURRENCY} = {exchange_rate:.4f} {memory['preferred_currency']}",
                            f"Tool 3: add_converted_revenue(target_currency='{memory['preferred_currency']}')",
                            f"Memory: {'saved to Mem0' if mem0_saved else 'saved to local JSON fallback'}",
                        ]
                    )
                )
            st.dataframe(result, use_container_width=True)
            plot_result(result, memory["chart_type"])
        except Exception as exc:
            st.error(str(exc))

    with st.expander("Database schema"):
        st.code(
            """
customers(customer_id, name, region, segment)
products(product_id, product_name, category, unit_price)
orders(order_id, order_date, customer_id, product_id, quantity, discount)
            """.strip()
        )


if __name__ == "__main__":
    main()
