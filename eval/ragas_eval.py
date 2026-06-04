import argparse
import asyncio
import json
import re
import sqlite3
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "sales_data.db"
SCHEMA_DOCS_PATH = ROOT / "schema_docs.json"
GOLDEN_PATH = ROOT / "eval" / "golden.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_schema_docs() -> list[dict]:
    return json.loads(SCHEMA_DOCS_PATH.read_text(encoding="utf-8"))


def retrieve_schema_doc_ids(question: str, limit: int = 6) -> list[str]:
    docs = load_schema_docs()
    question_text = question.lower()
    question_tokens = set(re.findall(r"[a-z_]+", question_text))
    scored_docs = []
    for doc in docs:
        keywords = [keyword.lower() for keyword in doc.get("keywords", [])]
        keyword_score = sum(2 for keyword in keywords if keyword in question_text)
        content_tokens = set(re.findall(r"[a-z_]+", doc.get("content", "").lower()))
        token_score = len(question_tokens.intersection(content_tokens))
        score = keyword_score + token_score
        if doc.get("type") in {"table", "join"}:
            score += 1
        if score:
            scored_docs.append((score, doc))

    if not scored_docs:
        scored_docs = [(1, doc) for doc in docs if doc.get("type") in {"table", "join"}]

    return [
        doc["id"]
        for _, doc in sorted(scored_docs, key=lambda item: (-item[0], item[1].get("id", "")))[:limit]
    ]


def execute_sql_context(sql: str) -> str:
    if not sql or not sql.strip().lower().startswith("select"):
        return "Security context: the input was blocked because only SELECT queries are allowed."

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cursor = conn.execute(sql)
        columns = [description[0] for description in cursor.description or []]
        rows = cursor.fetchall()
    finally:
        conn.close()

    lines = [",".join(columns)]
    lines.extend(",".join(str(value) for value in row) for row in rows)
    return "SQL execution result:\n" + "\n".join(lines)


def build_ragas_samples(results_payload: dict, limit: int | None = None) -> list[dict]:
    cases = results_payload["results"][:limit]
    docs_by_id = {doc["id"]: doc for doc in load_schema_docs()}
    samples = []

    for case in cases:
        schema_doc_ids = retrieve_schema_doc_ids(case["input"])
        schema_contexts = [docs_by_id[doc_id]["content"] for doc_id in schema_doc_ids]
        sql = case.get("sql", "")
        execution_context = (
            case.get("blocked_reason")
            or execute_sql_context(sql)
        )
        retrieved_contexts = [
            *schema_contexts,
            f"Generated SQL:\n{sql}",
            execution_context,
        ]
        reference_sql = case.get("reference_sql", "")
        if reference_sql:
            reference_contexts = [
                f"Reference SQL:\n{reference_sql}",
                execute_sql_context(reference_sql),
            ]
        else:
            reference_contexts = [
                "Security context: the request should be blocked or refused because only SELECT queries are allowed."
            ]
        samples.append(
            {
                "id": case["id"],
                "user_input": case["input"],
                "response": "\n".join([f"SQL: {sql}", execution_context]),
                "retrieved_contexts": retrieved_contexts,
                "reference_contexts": reference_contexts,
            }
        )

    return samples


async def score_with_ragas(samples: list[dict]) -> list[dict]:
    # RAGAS imports optional LLM integrations at module import time. This eval uses
    # only the non-LLM context recall metric, so a missing VertexAI integration
    # should not block the metric.
    vertexai_module = "langchain_community.chat_models.vertexai"
    if vertexai_module not in sys.modules:
        shim = types.ModuleType(vertexai_module)
        shim.ChatVertexAI = object
        sys.modules[vertexai_module] = shim

    try:
        from ragas.dataset_schema import SingleTurnSample
        from ragas.metrics import NonLLMContextRecall
    except Exception as exc:
        raise SystemExit(
            "RAGAS is not installed. Run `pip install -r requirements.txt` first."
        ) from exc

    metric = NonLLMContextRecall()
    scored = []
    for sample in samples:
        ragas_sample = SingleTurnSample(
            retrieved_contexts=sample["retrieved_contexts"],
            reference_contexts=sample["reference_contexts"],
        )
        score = await metric.single_turn_ascore(ragas_sample)
        scored.append({**sample, "context_recall": round(float(score), 3)})
    return scored


def summarize(scored: list[dict]) -> dict:
    if not scored:
        return {"cases": 0, "avg_context_recall": 0.0}
    return {
        "cases": len(scored),
        "avg_context_recall": round(
            sum(item["context_recall"] for item in scored) / len(scored), 3
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=ROOT / "eval" / "results_optimized.json")
    parser.add_argument("--output", type=Path, default=ROOT / "eval" / "ragas_results.json")
    parser.add_argument("--limit", type=int, help="Optional small sample size for quick smoke runs.")
    args = parser.parse_args()

    if not DB_PATH.exists():
        sys.path.insert(0, str(ROOT))
        from create_db import create_database

        create_database(DB_PATH)

    results_payload = json.loads(args.results.read_text(encoding="utf-8"))
    samples = build_ragas_samples(results_payload, args.limit)
    scored = asyncio.run(score_with_ragas(samples))
    payload = {
        "metric": "RAGAS NonLLMContextRecall",
        "description": (
            "Measures whether retrieved analysis evidence "
            "(schema snippets, generated SQL, and SQL execution results) "
            "recalls reference SQL evidence for each golden case."
        ),
        "summary": summarize(scored),
        "results": scored,
    }
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
