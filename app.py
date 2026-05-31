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
SCHEMA_CONTEXT = """
Tables:
customers(customer_id INTEGER PRIMARY KEY, name TEXT, region TEXT, segment TEXT)
products(product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT, unit_price REAL)
orders(order_id INTEGER PRIMARY KEY, order_date TEXT, customer_id INTEGER, product_id INTEGER, quantity INTEGER, discount REAL)

Revenue formula:
SUM(orders.quantity * products.unit_price * (1 - orders.discount))

Join rules:
orders.customer_id = customers.customer_id
orders.product_id = products.product_id
""".strip()
SORT_ASC_KEYWORDS = ["least", "lowest", "smallest", "bottom", "worst", "minimum", "min"]
SORT_DESC_KEYWORDS = ["most", "highest", "largest", "top", "best", "maximum", "max"]

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


def generate_sql_from_question(question: str) -> dict:
    client = get_openai_client()
    if client is None:
        route = route_question_with_rules(question)
        return {
            "sql": sql_for_question(route, infer_sort_order(question)),
            "explanation": "OpenAI was unavailable, so the app used approved-tool fallback SQL.",
            "source": "fallback",
        }

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a careful text-to-SQL generator for a SQLite sales database. "
                    "Generate exactly one read-only SELECT query. "
                    "Use only the provided schema. Do not use INSERT, UPDATE, DELETE, DROP, ALTER, PRAGMA, or multiple statements. "
                    "Prefer clear aliases and include ORDER BY/LIMIT when the question asks for top, most, least, or lowest. "
                    f"\n\n{SCHEMA_CONTEXT}\n\n"
                    "Return only JSON with keys: sql, explanation."
                ),
            },
            {"role": "user", "content": question},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    payload = json.loads(response.choices[0].message.content)
    sql = format_sql(payload.get("sql", ""))
    audit_event(
        "tool_call",
        "text_to_sql_generator",
        "success",
        {"question": question, "sql": sql, "explanation": payload.get("explanation", "")},
    )
    return {"sql": sql, "explanation": payload.get("explanation", ""), "source": "openai"}


def repair_sql(question: str, bad_sql: str, error: str) -> dict:
    client = get_openai_client()
    if client is None:
        return {"sql": bad_sql, "explanation": "OpenAI unavailable; repair skipped."}

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Repair a SQLite SELECT query for the provided schema. "
                    "Return exactly one safe read-only SELECT query. Do not use multiple statements. "
                    f"\n\n{SCHEMA_CONTEXT}\n\n"
                    "Return only JSON with keys: sql, explanation."
                ),
            },
            {
                "role": "user",
                "content": f"Question: {question}\nBad SQL: {bad_sql}\nError: {error}",
            },
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    payload = json.loads(response.choices[0].message.content)
    sql = format_sql(payload.get("sql", ""))
    audit_event(
        "tool_call",
        "text_to_sql_repair",
        "success",
        {"question": question, "bad_sql": bad_sql, "repaired_sql": sql, "error": error},
    )
    return {"sql": sql, "explanation": payload.get("explanation", "")}


def self_check_sql(question: str, sql: str, df: pd.DataFrame) -> dict:
    client = get_openai_client()
    if client is None:
        return {"passes": True, "reason": "OpenAI unavailable; self-check skipped."}

    preview = df.head(5).to_dict(orient="records")
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a SQL result self-checker. Decide whether the SQL and result preview answer the user's question. "
                    "Check table choice, joins, aggregation, ordering direction, and whether the result columns are relevant. "
                    "Return only JSON with keys: passes, reason."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\nSQL: {sql}\nRows returned: {len(df)}\n"
                    f"Result preview JSON: {json.dumps(preview)}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    payload = json.loads(response.choices[0].message.content)
    passes = bool(payload.get("passes"))
    audit_event(
        "tool_call",
        "sql_self_check",
        "success" if passes else "warning",
        {"question": question, "sql": sql, "passes": passes, "reason": payload.get("reason", "")},
    )
    return {"passes": passes, "reason": payload.get("reason", "")}


def execute_text_to_sql(question: str, initial_sql: str | None = None, explanation: str = "") -> dict:
    if initial_sql is None:
        generated = generate_sql_from_question(question)
        sql = generated["sql"]
        source = generated["source"]
        explanation = generated["explanation"]
    else:
        sql = format_sql(initial_sql)
        source = "edited" if explanation else "generated"
    repaired = False

    try:
        df = run_query(sql)
    except Exception as exc:
        audit_event(
            "tool_call",
            "text_to_sql_execute",
            "retry",
            {"question": question, "sql": sql, "error": str(exc)},
        )
        repaired_payload = repair_sql(question, sql, str(exc))
        sql = repaired_payload["sql"]
        repaired = True
        df = run_query(sql)

    check = self_check_sql(question, sql, df)
    return {
        "sql": sql,
        "df": df,
        "explanation": explanation,
        "source": source,
        "repaired": repaired,
        "self_check": check,
    }


def route_question_with_llm(question: str) -> dict:
    client = get_openai_client()
    available_tools = list(SUGGESTED_QUESTIONS)

    if client is None:
        fallback_tool = route_question_with_rules(question)
        sort_order = infer_sort_order(question)
        audit_event(
            "tool_call",
            "llm_router",
            "fallback",
            {
                "reason": "OPENAI_API_KEY unavailable",
                "question": question,
                "routed_tool": fallback_tool,
                "sort_order": sort_order,
            },
        )
        return {
            "tool": fallback_tool,
            "sort_order": sort_order,
            "reason": "OpenAI was unavailable, so the app used keyword routing.",
        }

    prompt = {
        "role": "system",
        "content": (
            "You are a safe routing layer for a Data Analyst Agent. "
            "Choose exactly one approved analysis tool for the user's question. "
            "Also infer sort_order as 'asc' if the user asks for least, lowest, smallest, bottom, worst, or minimum. "
            "Infer sort_order as 'desc' if the user asks for most, highest, largest, top, best, or maximum. "
            "Do not generate SQL. Do not invent tools. "
            f"Approved tools: {', '.join(available_tools)}. "
            "Return only JSON with keys: tool, sort_order, reason."
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

    sort_order = payload.get("sort_order")
    if sort_order not in {"asc", "desc"}:
        sort_order = infer_sort_order(question)

    audit_event(
        "tool_call",
        "llm_router",
        "success",
        {
            "question": question,
            "routed_tool": tool,
            "sort_order": sort_order,
            "reason": payload.get("reason", ""),
        },
    )
    return {"tool": tool, "sort_order": sort_order, "reason": payload.get("reason", "")}


def route_question_with_rules(question: str) -> str:
    text = question.lower()
    if any(word in text for word in ["month", "monthly", "trend", "over time", "date"]):
        return "Monthly revenue"
    if any(word in text for word in ["product", "products", "item", "items"]):
        return "Top products"
    if any(word in text for word in ["segment", "customer type", "enterprise", "retail"]):
        return "Customer segments"
    return "Revenue by region"


def infer_sort_order(question: str) -> str:
    text = question.lower()
    if any(keyword in text for keyword in SORT_ASC_KEYWORDS):
        return "asc"
    if any(keyword in text for keyword in SORT_DESC_KEYWORDS):
        return "desc"
    return "desc"


def sql_for_question(tool_name: str, sort_order: str = "desc") -> str:
    sql = SUGGESTED_QUESTIONS[tool_name]
    if sort_order == "asc":
        return format_sql(re.sub(r"ORDER BY revenue DESC", "ORDER BY revenue ASC", sql, flags=re.IGNORECASE))
    return format_sql(re.sub(r"ORDER BY revenue ASC", "ORDER BY revenue DESC", sql, flags=re.IGNORECASE))


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


def format_sql(sql: str) -> str:
    formatted = normalize_sql(sql)
    formatted = re.sub(r"\bFROM\b", "\nFROM", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bLEFT\s+JOIN\b", "\nLEFT JOIN", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bJOIN\b", "\nJOIN", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bWHERE\b", "\nWHERE", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bGROUP\s+BY\b", "\nGROUP BY", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bHAVING\b", "\nHAVING", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bORDER\s+BY\b", "\nORDER BY", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"\bLIMIT\b", "\nLIMIT", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r",\s*(?=[A-Za-z_][A-Za-z0-9_.]*\b)", ",\n       ", formatted)
    return formatted.strip()


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
    st.markdown(
        """
        <style>
        .block-container {
            max-width: 980px;
            padding-left: 3rem;
            padding-right: 2rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    memory = load_memory()
    st.session_state.setdefault("generated_question", DEFAULT_USER_QUESTION)
    st.session_state.setdefault("generated_route", None)
    st.session_state.setdefault("preferred_currency", memory.get("preferred_currency", "CAD"))

    header_left, header_right = st.columns([3, 1])
    with header_left:
        st.title("Data Analyst Agent")
    with header_right:
        st.selectbox(
            "Preferred currency",
            SUPPORTED_CURRENCIES,
            key="preferred_currency",
        )
        memory["preferred_currency"] = st.session_state["preferred_currency"]
        save_memory(memory)

    with st.sidebar:
        st.caption("Capstone components: Tools, Memory, and Security")
        st.header("Memory")
        st.caption(mem0_status())
        chart_type = st.radio(
            "Preferred chart",
            ["bar", "line"],
            index=["bar", "line"].index(memory.get("chart_type", "bar")),
        )
        memory["chart_type"] = chart_type
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

    user_question = st.text_input("Ask a business question", value=st.session_state["generated_question"])
    agent_mode = st.radio(
        "Agent mode",
        ["Text-to-SQL", "Approved tool router"],
        horizontal=True,
    )
    generate_clicked = st.button("Generate", type="secondary")
    if generate_clicked and agent_mode == "Approved tool router":
        st.session_state["generated_question"] = user_question
        st.session_state["generated_route"] = route_question(user_question)

    manual_mode = st.checkbox("Choose analysis tool manually", disabled=agent_mode == "Text-to-SQL")

    if agent_mode == "Text-to-SQL":
        if generate_clicked:
            st.session_state["generated_question"] = user_question
            generated = generate_sql_from_question(user_question)
            st.session_state["text_to_sql_payload"] = generated
        elif "text_to_sql_payload" not in st.session_state:
            st.session_state["text_to_sql_payload"] = generate_sql_from_question(user_question)

        user_question = st.session_state["generated_question"]
        question = "Text-to-SQL"
        sort_order = "generated"
        route_reason = st.session_state["text_to_sql_payload"]["explanation"]
        generated_sql = st.session_state["text_to_sql_payload"]["sql"]
        st.info("OpenAI generated a SELECT query from your natural-language question.")
    elif manual_mode:
        question = st.selectbox("Choose an analysis tool", list(SUGGESTED_QUESTIONS))
        sort_order = st.radio("Sort revenue", ["desc", "asc"], horizontal=True)
        route_reason = "Manual tool selection."
        generated_sql = sql_for_question(question, sort_order)
    else:
        if st.session_state["generated_route"] is None:
            st.session_state["generated_route"] = route_question(st.session_state["generated_question"])
        route = st.session_state["generated_route"]
        user_question = st.session_state["generated_question"]
        question = route["tool"]
        sort_order = route.get("sort_order", "desc")
        route_reason = route["reason"]
        generated_sql = sql_for_question(question, sort_order)
        st.info(f"LLM routed this question to: {question} ({sort_order.upper()} revenue)")

    st.caption("Click Generate after changing the question. The SQL below updates from the natural-language request.")
    sql = st.text_area(
        "SQL generated by the agent",
        generated_sql,
        height=180,
        key=f"sql_{agent_mode}_{question}_{sort_order}_{hash(generated_sql)}",
    )

    if st.button("Run analysis", type="primary"):
        try:
            if agent_mode == "Text-to-SQL":
                text_to_sql_result = execute_text_to_sql(user_question, sql, route_reason)
                sql = text_to_sql_result["sql"]
                result = text_to_sql_result["df"]
                self_check = text_to_sql_result["self_check"]
                repaired = text_to_sql_result["repaired"]
            else:
                result = run_query(sql)
                self_check = {"passes": True, "reason": "Approved template SQL was used."}
                repaired = False

            memory["preferred_currency"] = st.session_state["preferred_currency"]
            exchange_rate = fetch_exchange_rate(DEFAULT_BASE_CURRENCY, st.session_state["preferred_currency"])
            result = add_converted_revenue(result, st.session_state["preferred_currency"], exchange_rate)
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
                            f"Agent mode: {agent_mode}",
                            f"Tool 0: {'text_to_sql_generator' if agent_mode == 'Text-to-SQL' else f'llm_router -> {question} ({sort_order.upper()})'}",
                            f"Routing reason: {route_reason}",
                            "Tool 1: query_database(sql)",
                            f"SQL repaired once: {repaired}",
                            f"Self-check passed: {self_check['passes']}",
                            f"Self-check reason: {self_check['reason']}",
                            f"Tool 2: fetch_exchange_rate(base='{DEFAULT_BASE_CURRENCY}', target='{st.session_state['preferred_currency']}')",
                            f"Exchange rate: 1 {DEFAULT_BASE_CURRENCY} = {exchange_rate:.4f} {st.session_state['preferred_currency']}",
                            f"Tool 3: add_converted_revenue(target_currency='{st.session_state['preferred_currency']}')",
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
