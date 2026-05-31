# Data Analyst Agent

This project is a capstone prototype for **#6 Data Analyst Agent** using three required components:

1. **Tools**: the agent uses function-style tools to query data, call an external exchange-rate API, convert revenue, summarize results, and draw charts.
2. **Memory**: the app uses Mem0 for persistent cross-session memory when `OPENAI_API_KEY` is set, with `memory.json` as a local fallback for demo reliability.
3. **Security / Governance**: the SQL layer only accepts read-only `SELECT` queries, blocks dangerous keywords, opens SQLite in read-only mode, restricts external API calls to an allowlisted host, and writes audit events for tool calls and blocked attempts.

## Setup

```bash
pip install -r requirements.txt
python create_db.py
streamlit run app.py
```

To enable Mem0 memory, set your OpenAI API key before starting Streamlit:

```powershell
$env:OPENAI_API_KEY="your-openai-api-key"
streamlit run app.py
```

Do not hard-code API keys in this repository.

## Demo Flow

1. Run `python create_db.py` to create `sales_data.db`.
2. Start the app with `streamlit run app.py`.
3. Pick an analysis tool such as `Revenue by region`.
4. Pick a preferred currency in the sidebar, such as `CAD` or `CNY`.
5. Run the query and show the table, chart, summary, and tool call trace.
6. Change the chart or currency preference in the sidebar to demonstrate memory.
7. Try a blocked query such as `DROP TABLE orders` to demonstrate security.
8. Open `Recent audit events` in the sidebar to demonstrate governance logging.

## Component Mapping

| Capstone Component | Where it appears |
| --- | --- |
| Tools | `run_query`, `fetch_exchange_rate`, `add_converted_revenue`, `summarize`, `plot_result` in `app.py` |
| Memory | Mem0 via `get_mem0_memory`, `add_mem0_memory`, `search_mem0_memories`; `memory.json` fallback; preferred chart and preferred currency |
| Security / Governance | `validate_sql`, blocked SQL keywords, SQLite read-only connection, `validate_external_api_url`, API host allowlist, `audit_event`, `audit_events.jsonl` |

## External API Tool

The agent calls the free Frankfurter exchange-rate API:

```text
https://api.frankfurter.dev/v2/rates?base=USD&quotes=CAD
```

This supports the Tools requirement because the agent performs a function-style external API call after querying local sales data.

## Mem0 Memory

The agent stores long-term user preferences and previous analysis context in Mem0:

```text
User prefers CAD currency and bar charts. Last data analysis request: Revenue by region.
```

On later app sessions, the agent searches Mem0 for relevant user preferences and displays retrieved memories in the sidebar. If Mem0 is unavailable, the app still keeps basic cross-session memory in `memory.json`.

## Security / Governance

The app records governance events in `audit_events.jsonl`. Each event includes a timestamp, event type, tool name, status, and details.

Example successful tool call:

```json
{"event_type": "tool_call", "tool": "query_database", "status": "success"}
```

Example blocked attempt:

```json
{"event_type": "security_block", "tool": "query_database", "status": "blocked"}
```

## Example Safe Query

```sql
SELECT c.region,
       ROUND(SUM(o.quantity * p.unit_price * (1 - o.discount)), 2) AS revenue
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN products p ON o.product_id = p.product_id
GROUP BY c.region
ORDER BY revenue DESC;
```
