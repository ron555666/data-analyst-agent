# Data Analyst Agent

This project is a capstone prototype for **#6 Data Analyst Agent** using three required components:

1. **Tools**: the agent uses OpenAI for simplified text-to-SQL and safe tool routing, then queries data, calls an external exchange-rate API, converts revenue, summarizes results, and draws charts.
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
3. Ask a natural-language question such as `Which region generated the most revenue?`.
4. Click `Generate` to create a safe `SELECT` query from the natural-language question.
5. Pick a preferred currency in the sidebar, such as `CAD` or `CNY`.
6. Run the query and show the table, chart, summary, and tool call trace.
7. Change the chart or currency preference in the sidebar to demonstrate memory.
8. Try a blocked query such as `DROP TABLE orders` to demonstrate security.
9. Open `Recent audit events` in the sidebar to demonstrate governance logging.

## Component Mapping

| Capstone Component | Where it appears |
| --- | --- |
| Tools | `generate_sql_from_question`, `repair_sql`, `self_check_sql`, `route_question_with_llm`, `run_query`, `fetch_exchange_rate`, `add_converted_revenue`, `summarize`, `plot_result` in `app.py` |
| Memory | Mem0 via `get_mem0_memory`, `add_mem0_memory`, `search_mem0_memories`; `memory.json` fallback; preferred chart and preferred currency |
| Security / Governance | `validate_sql`, blocked SQL keywords, SQLite read-only connection, `validate_external_api_url`, API host allowlist, `audit_event`, `audit_events.jsonl` |

## External API Tool

The agent calls the free Frankfurter exchange-rate API:

```text
https://api.frankfurter.dev/v2/rates?base=USD&quotes=CAD
```

This supports the Tools requirement because the agent performs a function-style external API call after querying local sales data.

The default mode is simplified text-to-SQL:

```text
Natural-language question
-> OpenAI generates one SELECT query
-> validate_sql checks safety
-> SQLite executes in read-only mode
-> OpenAI repairs the SQL once if execution fails
-> OpenAI self-checks whether the SQL/result answer the question
```

The fallback router mode never executes arbitrary LLM SQL. It only routes the natural-language question to one approved tool from the allowlist:

```text
Revenue by region
Top products
Monthly revenue
Customer segments
```

It can also infer safe sort intent. For example, `Which region generated the least revenue?` keeps the same approved analysis tool but changes the SQL ordering from `ORDER BY revenue DESC` to `ORDER BY revenue ASC`.

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

## Evaluation

The eval harness follows the capstone's Four Builds structure:

```bash
python eval/run_eval.py --mode baseline --output eval/results_baseline.json
python eval/run_eval.py --mode optimized --output eval/results_optimized.json
python eval/judge.py --results eval/results_optimized.json --human-labels eval/human_labels.json
pytest tests/unit -q
```

Current saved results:

| Mode | Pass rate | Avg expected coverage | Avg forbidden rate |
| --- | ---: | ---: | ---: |
| Baseline approved-tool router | 0.48 | 0.537 | 0.08 |
| Optimized text-to-SQL + repair + self-check | 0.64 | 0.643 | 0.00 |

LLM judge agreement against manual labels: `Cohen's kappa = 0.603`.

See `eval/experiment_log.md` for the experiment table, failure analysis, and trade-off.

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
