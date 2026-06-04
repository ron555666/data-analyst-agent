# Experiment Log

| Round | Change | Metric Delta | Conclusion |
| --- | --- | --- | --- |
| 0 | Baseline approved-tool router only | Baseline result consistency target: lower coverage on non-template questions | Stable and safe, but too narrow for flexible data questions. |
| 1 | Added OpenAI text-to-SQL over the SQLite schema | Expected coverage improves on category, customer, and filter questions | More flexible than four fixed templates. |
| 2 | Added one repair retry after SQL execution errors | Execution success improves on malformed generated SQL | Small latency/cost increase, better reliability. |
| 3 | Added SQL/result self-check and audit logging | Governance visibility improves; failures become easier to inspect | Better presentation story and safer operation. |
| 4 | Added lightweight schema RAG over `schema_docs.json` | Improves schema grounding and aligns the data analyst agent with Tools + RAG + self-check | Deterministic retrieval is enough for this small schema and easier to govern than a vector store. |

## Result Summary

Run these commands to reproduce the result tables:

```bash
python eval/run_eval.py --mode baseline
python eval/run_eval.py --mode optimized
python eval/ragas_eval.py --results eval/results_optimized.json --output eval/ragas_results.json
python eval/judge.py --results eval/results_optimized.json --human-labels eval/human_labels.json
```

Current saved deterministic run:

| Mode | Pass rate | Avg expected coverage | Avg forbidden rate |
| --- | ---: | ---: | ---: |
| Baseline approved-tool router | 0.48 | 0.537 | 0.08 |
| Optimized text-to-SQL + self-check | 0.64 | 0.643 | 0.00 |

Current RAGAS semantic metric: `NonLLMContextRecall = 0.96` over 25 optimized cases.

Current LLM-judge agreement against manual labels: `kappa = 0.603`.

RAGAS semantic metric: `eval/ragas_eval.py` runs `NonLLMContextRecall` on retrieved analysis evidence against reference SQL evidence for each golden case.

## Failure Analysis

1. Forecasting questions can fail because the database has historical sales rows but no forecasting model. I do not treat this as a supported query type.
2. City-level questions can fail because the schema has `region` but no `city` column. The self-check should catch that the result cannot answer the question directly.
3. Weather or marketing-spend questions can fail because those external variables are not present in the local database. This is a data coverage limitation, not only an LLM issue.

## Trade-Off

The optimized version trades higher latency and small OpenAI cost for broader question coverage, repair ability, and explicit self-checking.
