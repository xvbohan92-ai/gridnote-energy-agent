# Local Qwen3 integration

This is the pre-training Ollama integration record. The subsequently completed weight training uses a separate MLX checkpoint and service; see [QLoRA experiment and reproduction](../training/README.md). Do not combine the request counts or smoke results in this document with that experiment.

The installed model was `qwen3:4b`, Q4_K_M, 4.0B parameters, about 2.50 GB, served by Ollama 0.34.2. Its metadata declares completion, tools and thinking capabilities. A one-function Responses probe returned the requested function, but the original full multi-tool prompts did not complete reliably. Connectivity alone was not treated as agent accuracy.

## Verified final behavior

| Task | Result | Model requests | Individual local elapsed time |
| --- | --- | --- | --- |
| Last seven days of daily PJM load | 7 rows, domain numeric check passed | 2 | 3,376.54 ms |
| Last seven days of PJM record count, through the live UI | 168 records, domain numeric check passed | 2 | 2,037.67 ms |

Both completed tasks selected `calculate_metric`, received its actual output and selected `finish_analysis(e1)`. The calculator compiles the documented finite task into SQL and checks the results independently. These examples verify model-driven tool selection and observation replay. They do **not** establish free-form model-generated SQL quality, general accuracy, or training gains.

The complete debugging session used 13 local generation requests, including failed attempts and diagnostic probes. No OpenAI/cloud model requests were sent. Local API fees are zero; hardware and electricity costs are not estimated. Timings are individual warm local observations, not a P50/P95 benchmark or SLA. The questions were used during development and were not held out.

[Structured verification summary](../reports/ollama-verification.json) includes the unsuccessful integration attempts. Raw local traces remain ignored to protect future user datasets. A screenshot of the actual UI query is in [ollama-local-query.png](ollama-local-query.png).

## What changed

The installed template always ended the assistant prefix with an opening `<think>` tag. A dedicated `gridnote-qwen3:4b` profile closes this prefix for non-thinking use while reusing the existing weights. The original `qwen3:4b` profile was not overwritten. This changes a prompt template, not weights; it is not SFT, LoRA or post-training. Template changes alone did not make the original native multi-tool flow pass.

The local backend therefore uses Ollama's native `/api/chat` JSON Schema output mode. The schema offers five typed actions: inspect, SQL query, finite metric calculation, plot, and finish. The model chooses an action and arguments; the adapter validates the action and passes it to the same trusted runtime used by the cloud tool loop. Action results are returned to the model as untrusted observations. Internal `function_call` events normalize the runtime interface; they are **not** presented as native Ollama function-call output.

An early JSON run generated `DATE_TRUNC`, repaired it to SQLite `DATE` after an actual error, but still omitted required columns and exhausted its three-request budget. Inspection exposed a missing required-column contract in the new interface. The interface now supplies that contract and identifies the available finite metric tool without revealing a gold SQL answer. The local instruction recommends the calculator for finite supported tasks and permits it as an alternative to repeatedly failing SQL.

All query scope, read-only authorization, semantic checks, evidence IDs, result limits and request limits still apply. A local HTTP failure or invalid action does not trigger a cloud fallback. URLs are restricted to loopback addresses, redirects and environment proxies are disabled, cloud model tags are rejected, and OpenAI credentials are not forwarded to Ollama.

Several changes were made together: prompt template, decoding constraints, concise instructions and task metadata. No isolated causal attribution is claimed. The earlier failed 45–50 second attempts cannot serve as a baseline for claiming a speedup against a successful final task.

## Reproduce

With Ollama already serving and `qwen3:4b` installed:

```bash
ollama create gridnote-qwen3:4b -f models/qwen3-gridnote.Modelfile
```

This uses the existing weight blobs and adds a small project profile; no weight download or training is required. Do not overwrite an unrelated existing profile with the same name. The profile and base template hashes are recorded in the verification summary.

Set these variables in the local ignored `.env`:

```dotenv
ENERGY_PLANNER=ollama-tools
ENERGY_DB_PATH=data/eia-pjm.sqlite
OLLAMA_MODEL=gridnote-qwen3:4b
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
ENERGY_MAX_MODEL_CALLS=3
```

The `/v1` base setting is accepted for configuration compatibility; the local adapter uses the same origin's native `/api/chat`. It does not require an OpenAI key. Restart the service after changing configuration, then explicitly run:

```bash
uv run energy-agent verify-model --provider ollama --max-requests 3
uv run energy-agent serve --port 8765
```

The home-page preview remains a free fixed query with zero model calls. The UI badge identifies the configured local profile; completed requests show actual model-call and token counts. `energy-agent evaluate` still defaults to offline rule checks even if `.env` selects Ollama, so ordinary batch checks do not unexpectedly run inference.

Official references: [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs), [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling), [Ollama model creation](https://docs.ollama.com/api/create), and [Qwen3-4B model card](https://huggingface.co/Qwen/Qwen3-4B).
