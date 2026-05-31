import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from create_db import create_database  # noqa: E402
from app import (  # noqa: E402
    DB_PATH,
    execute_text_to_sql,
    route_question_with_rules,
    run_query,
    sql_for_question,
    validate_sql,
)


GOLDEN_PATH = ROOT / "eval" / "golden.jsonl"


def load_cases(path: Path = GOLDEN_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def answer_text(df, sql: str, blocked_reason: str = "") -> str:
    parts = [sql, blocked_reason]
    if df is not None:
        parts.append(df.to_csv(index=False))
    return "\n".join(part for part in parts if part).lower()


def tokens_from_fact(fact: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+\.\d+|\d{4}-\d{2}", fact.lower())
    stop = {
        "the",
        "is",
        "are",
        "about",
        "query",
        "answer",
        "revenue",
        "generated",
        "highest",
        "lowest",
        "most",
        "least",
        "by",
        "has",
        "in",
        "from",
        "to",
        "with",
        "appears",
        "before",
        "higher",
        "filters",
        "filter",
        "or",
        "otherwise",
        "identifies",
        "minimum",
    }
    return [token for token in tokens if token not in stop and not re.fullmatch(r"\d+\.\d+|\d+", token)]


def numbers_from(text: str) -> list[float]:
    return [float(value) for value in re.findall(r"\d+\.\d+|\b\d+\b", text)]


def number_present(expected: float, text: str) -> bool:
    return any(abs(expected - actual) <= 0.05 for actual in numbers_from(text))


def fact_present(fact: str, text: str) -> bool:
    expected_numbers = numbers_from(fact)
    if expected_numbers and not all(number_present(number, text) for number in expected_numbers):
        return False

    tokens = tokens_from_fact(fact)
    if not tokens:
        return False
    matches = sum(1 for token in tokens if token in text)
    return matches / len(tokens) >= 0.6


def forbidden_present(fact: str, text: str) -> bool:
    lowered = fact.lower()
    exact_risks = ["delete from", "drop table", "orders are deleted"]
    if any(risk in lowered for risk in exact_risks):
        return lowered in text

    ranking_words = ["highest", "lowest", "top", "most", "least"]
    if any(word in lowered for word in ranking_words) and not numbers_from(fact):
        return False
    if not numbers_from(fact):
        return False

    return fact_present(fact, text)


def score_case(case: dict, text: str) -> dict:
    expected = case["expected_facts"]
    forbidden = case["forbidden_facts"]
    expected_hits = [fact for fact in expected if fact_present(fact, text)]
    forbidden_hits = [fact for fact in forbidden if forbidden_present(fact, text)]
    expected_coverage = len(expected_hits) / len(expected) if expected else 1.0
    forbidden_rate = len(forbidden_hits) / len(forbidden) if forbidden else 0.0
    passed = expected_coverage >= 0.6 and forbidden_rate == 0.0
    return {
        "expected_coverage": round(expected_coverage, 3),
        "forbidden_rate": round(forbidden_rate, 3),
        "passed": passed,
        "expected_hits": expected_hits,
        "forbidden_hits": forbidden_hits,
    }


def baseline_sql(case: dict) -> str:
    routed_tool = route_question_with_rules(case["input"])
    sort_order = "asc" if any(word in case["input"].lower() for word in ["least", "lowest"]) else "desc"
    return sql_for_question(routed_tool, sort_order)


def run_case(case: dict, mode: str, use_reference_sql: bool = False) -> dict:
    if "security" in case["tags"]:
        ok, reason = validate_sql(case["input"])
        text = answer_text(None, case["input"], reason)
        scored = score_case(case, text)
        return {**case, "mode": mode, "sql": case["input"], "blocked": not ok, **scored}

    if use_reference_sql:
        sql = case["reference_sql"]
        df = run_query(sql)
    elif mode == "baseline":
        sql = baseline_sql(case)
        df = run_query(sql)
    else:
        result = execute_text_to_sql(case["input"])
        sql = result["sql"]
        df = result["df"]

    scored = score_case(case, answer_text(df, sql))
    return {**case, "mode": mode, "sql": sql, "rows": len(df), **scored}


def summarize(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for result in results if result["passed"])
    return {
        "cases": total,
        "pass_rate": round(passed / total, 3),
        "avg_expected_coverage": round(sum(r["expected_coverage"] for r in results) / total, 3),
        "avg_forbidden_rate": round(sum(r["forbidden_rate"] for r in results) / total, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["baseline", "optimized"], default="baseline")
    parser.add_argument("--use-reference-sql", action="store_true", help="Run deterministic reference SQL instead of model-generated SQL.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not DB_PATH.exists():
        create_database()

    cases = load_cases()
    results = [run_case(case, args.mode, args.use_reference_sql) for case in cases]
    payload = {"summary": summarize(results), "results": results}

    output = args.output or ROOT / "eval" / f"results_{args.mode}.json"
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
