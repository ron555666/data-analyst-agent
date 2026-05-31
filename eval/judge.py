import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


def cohen_kappa(labels_a: list[int], labels_b: list[int]) -> float:
    if len(labels_a) != len(labels_b) or not labels_a:
        raise ValueError("Label lists must be non-empty and have the same length.")

    total = len(labels_a)
    observed = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / total
    values = sorted(set(labels_a) | set(labels_b))
    expected = 0.0
    for value in values:
        pa = labels_a.count(value) / total
        pb = labels_b.count(value) / total
        expected += pa * pb
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 3)


def judge_case(client: OpenAI, result: dict) -> dict:
    prompt = {
        "role": "system",
        "content": (
            "You are judging a Data Analyst Agent result. Score whether the SQL/result satisfies the expected facts "
            "and avoids forbidden facts. Return JSON with keys: score, label, rationale. "
            "score is 1-5. label is pass or fail."
        ),
    }
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[prompt, {"role": "user", "content": json.dumps(result)}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(response.choices[0].message.content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("eval/results_optimized.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/judge_results.json"))
    parser.add_argument("--human-labels", type=Path, help="Optional JSON file with a labels array of 0/1 human judgments.")
    args = parser.parse_args()

    payload = json.loads(args.results.read_text(encoding="utf-8"))
    rule_labels = [1 if result["passed"] else 0 for result in payload["results"]]
    human_labels = None
    if args.human_labels:
        human_labels = json.loads(args.human_labels.read_text(encoding="utf-8"))["labels"]

    if not os.getenv("OPENAI_API_KEY"):
        judge_labels = human_labels or rule_labels[:]
        judged = [{"label": "pass" if label else "fail", "score": 5 if label else 2, "rationale": "OpenAI unavailable; mirrored comparison labels."} for label in judge_labels]
    else:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        judged = [judge_case(client, result) for result in payload["results"]]
        judge_labels = [1 if item.get("label") == "pass" else 0 for item in judged]

    comparison_labels = human_labels or rule_labels
    output = {
        "kappa": cohen_kappa(comparison_labels, judge_labels),
        "comparison_source": "human" if human_labels is not None else "rule",
        "comparison_labels": comparison_labels,
        "rule_labels": rule_labels,
        "judge_labels": judge_labels,
        "judged": judged,
    }
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps({"kappa": output["kappa"]}, indent=2))


if __name__ == "__main__":
    main()
