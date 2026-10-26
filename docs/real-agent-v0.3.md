# Gridnote v0.3 — implementation and evidence

This document records the initial real-data and Ollama integration checkpoint (138 tests). The later completed MLX QLoRA experiment, 149-test checkpoint, paired model scores and remaining failures are documented in [the training report](../training/README.md). The evidence below remains a historical record of the tool integration, not the fine-tuning result.

## What is verified

| Evidence | Result | Meaning |
| --- | --- | --- |
| Actual EIA-930 PJM reported demand | 8,737 hourly records in UTC year 2024 | Real source observations, not generated examples |
| Calendar coverage | 8,737 / 8,784 hours; 47 missing | Missing demand is omitted and audited, not zero-filled |
| Forecast availability | 8,736 / 8,737 retained records | One retained actual has no forecast; NULL stays NULL |
| Source/database integrity | 8,737 rows exactly equal | A separate CSV decoder rechecks timestamp and both values |
| Rule contract pilot | Dev 12/12, candidate test 24/24 | Engineering calculations on real data; not LLM accuracy |
| Automated tests | 138 passed after local integration | Includes mocked SDK/action protocols and failure boundaries |
| Real provider requests | OpenAI 0; Ollama 13 including debugging | Two local smoke tasks completed; no accuracy benchmark claimed |

The source snapshots, report JSON, daily CSV and PNG/SVG are retained. Raw downloaded files, local databases, `.env` and real-model traces remain ignored. EIA permits reuse of its data with acknowledgment; no EIA logo is included. Historical files can change: match all three SHA-256 values before comparing results.

## Time and value definitions

The source columns are `UTC Time at End of Hour`, `Demand (MW)` and `Demand Forecast (MW)`. The adapter subtracts exactly one hour from the UTC hour-ending timestamp and then selects `[2024-01-01T00:00:00Z, 2025-01-01T00:00:00Z)`. It never attempts to infer UTC from ambiguous local clock values. Since six-month files use local reporting dates, the previous year's July–December file supplies the first UTC hours.

Original reported demand is used. Adjusted and imputed columns are deliberately unused. Missing demand rows are counted and excluded; absent forecasts remain NULL. Non-finite or negative values abort the import. Identical duplicate hours are audited once; conflicting duplicates abort without replacing the database. The 47 missing hours cluster around March 10–11 and November 3–4, but the project does not infer a cause from that coincidence.

`forecast_mw` is EIA's reported forecast snapshot, not a model trained by this project, and not a guarantee that every forecast was available before its target hour. MAE/MAPE here describe reported pairs; they are not a rigorous as-of forecasting backtest.

## Tool loop

`ENERGY_PLANNER=openai-tools` enables a bounded Responses loop:

1. Resolve a single region and UTC date scope on the server. Known absent data and invalid scopes stop before model calls.
2. Give the model the schema and limited deterministic task contract, without a gold SQL answer.
3. Let the model select `inspect_dataset`, `query_load`, `calculate_metric`, `plot_result` or `finish_analysis`.
4. Return real tool observations, including actionable SQL/semantic errors. SQL executes through the existing LangGraph tool with AST scoping, SQLite authorization and independent metric checks in domain mode.
5. Publish only evidence explicitly selected from the current run. Plain prose, invented IDs, failed queries and budget-exhausted partial results are not published as completed answers.

`calculate_metric` uses the finite task's trusted SQL definition. A run using this tool demonstrates model tool selection, not model-generated SQL quality. Open-ended SQL results are marked semantically uncovered. The regex contract parser remains limited and is not evidence of general intent understanding. The final numeric summary is generated locally; the system does not yet synthesize an unrestricted model-written report.

Default limits: 3 model requests, no SDK retries, 12 tool calls, 65,000 transcript characters, SQL 2 seconds / 2 million VM instructions, 500 displayed result rows, 20 preview rows per tool observation. Tool results, row cells and errors are untrusted inputs to the model. No arbitrary Python, shell, file write or external network tool is available to the model. API/provider failures stay failures and never become successful refusals or offline fallbacks.

The `baseline`, `agent`, and `domain` execution modes control SQL preflight/repair/domain validation in the original workflow. Merely selecting these modes in the new loop does not create a fair one-shot-versus-agent experiment: the outer loop still has the same tool-call affordances. A future model comparison must explicitly fix model version, tools, safety limits, data, request budgets and scoring, and disclose which component differs.

## Reproduce without model calls

From the repository root:

```bash
uv sync --frozen --group viz
ENERGY_DB_PATH=data/eia-pjm.sqlite uv run energy-agent import-eia --year 2024 --download
ENERGY_DB_PATH=data/eia-pjm.sqlite uv run energy-agent validate-real --figures docs/figures
uv run pytest -q
uv run ruff check energy_agent tests
node --check energy_agent/static/app.js
```

The importer requires `--replace` to overwrite an existing target. It does not refresh existing source snapshots. An intentional source update requires new fingerprints and a fresh report rather than editing a saved fingerprint to accept changed data. A source mismatch or failed contract check must be investigated; `validate-real` exits nonzero on failed checks.

The oracle in `real_validation.py` decodes the source CSV independently and does not import production metric helpers, task compilers or EIA conversion functions. It scores values, columns, row counts, order and NULL behavior, while ignoring extra presentation columns. Its scope is calculation correctness. The 36 questions are authored alongside this implementation; dev dates are January–March and candidate test dates July–December. Both sets share metric families, and there has been no external question/label review. They are a reviewable pilot, not an independent public benchmark or training dataset.

## Cloud validation deferred; local integration verified

OpenAI cloud validation remains deferred; no cloud model requests were made. Local Qwen3 integration is now verified on two basic tasks, with prior failed attempts retained. See [local setup and limitations](ollama-local.md) and [the verification summary](../reports/ollama-verification.json). Local mode uses schema-constrained actions through native Ollama, not Responses function calls. The model selects tools; the server computes and verifies the finite metrics. It is not evidence of correct free-form SQL generation or model training.

To validate OpenAI later, locally set `OPENAI_API_KEY`, a supported `OPENAI_MODEL`, and `ENERGY_DB_PATH=data/eia-pjm.sqlite` in ignored `.env`. Do not paste credentials into chat or commit them. Changes take effect after restarting the server.

```bash
uv run energy-agent verify-model --provider openai --max-requests 3
```

This is an explicit paid call path for one daily-load task, with at most three requests and no automatic SDK retries. It saves `reports/model-smoke.json`, ignored by default. Review data and provider request usage before sharing that trace. One successful task would verify connectivity and tool protocol only, not accuracy or improvement. A subsequent small-scale SQL-component QLoRA experiment is now complete; full independent LLM evaluation, anomaly-quality repair and production access controls remain future work.

Official implementation references: [Responses function calling](https://developers.openai.com/api/docs/guides/function-calling), [LangGraph workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents), [EIA Open Data](https://www.eia.gov/opendata/) and [EIA reuse policy](https://www.eia.gov/about/copyrights_reuse.php).
