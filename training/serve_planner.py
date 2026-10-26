"""Single-worker local MLX planner; model generates SQL, the app executes and verifies it."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from common import ROOT, sha, verify_frozen
from inference import generate_plan
from mlx_lm import load
from pydantic import BaseModel, ConfigDict, Field

from energy_agent.models import AnalysisTask, QueryPlan, Scope
from energy_agent.sql_sft import messages


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=2000)
    scope: Scope
    task: AnalysisTask
    previous_attempt: dict | None = None


def main():
    verify_frozen()
    trained = json.loads((ROOT / "reports/sft-training.json").read_text())
    config = trained["config"]
    assert sha(ROOT / config["best_path"] / "adapters.safetensors") == trained["selected_adapter_sha256"]
    model, tokenizer = load(
        ROOT / config["model"],
        adapter_path=str(ROOT / config["best_path"]),
        tokenizer_config={"trust_remote_code": False},
        trust_remote_code=False,
    )
    label = f"Qwen3-4B QLoRA · step {trained['best_checkpoint']['iteration']}"

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/health":
                return self.reply(404, {"detail": "Not found"})
            self.reply(
                200, {"status": "ready", "model": label, "adapter_sha256": trained["selected_adapter_sha256"]}
            )

        def do_POST(self):
            if self.path != "/plan":
                return self.reply(404, {"detail": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 32768:
                    return self.reply(413, {"detail": "Request body must be at most 32 KiB"})
                request = Request.model_validate_json(self.rfile.read(length))
                if request.task.kind in ("custom", "anomaly"):
                    return self.reply(422, {"detail": "Only the finite trained SQL contracts are supported"})
                # Scope note is explanatory UI text, excluded from the model's versioned input protocol.
                scope = request.scope.model_copy(update={"note": ""})
                text, timing = generate_plan(
                    model,
                    tokenizer,
                    messages(request.question, scope, request.task, request.previous_attempt),
                    config,
                )
                try:
                    plan = QueryPlan.model_validate_json(text.strip())
                except ValueError:
                    return self.reply(
                        200, {"plan": None, "model": label, "usage": timing, "error_code": "invalid_plan"}
                    )
                self.reply(
                    200,
                    {
                        "plan": plan.model_dump(),
                        "model": label,
                        "usage": timing,
                        "adapter_sha256": trained["selected_adapter_sha256"],
                    },
                )
            except ValueError:
                self.reply(422, {"detail": "Invalid planner request"})
            except Exception as exc:
                print(f"Local planner failed: {type(exc).__name__}", flush=True)
                self.reply(503, {"detail": "Local model inference failed"})

    print(json.dumps({"status": "ready", "url": "http://127.0.0.1:8768", "model": label}), flush=True)
    # Metal inference is serialized in this process; no concurrent model mutation or GPU calls.
    HTTPServer(("127.0.0.1", 8768), Handler).serve_forever()


if __name__ == "__main__":
    main()
