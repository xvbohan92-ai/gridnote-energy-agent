"""Run actual local QLoRA updates; select by validation loss, never test outputs."""

import importlib.metadata
import json
import shutil
import time
import types

import mlx.core as mx
import numpy as np
from common import ROOT, NoThinkingTokenizer, read_rows, sha, verify_frozen
from mlx.utils import tree_flatten
from mlx_lm import load
from mlx_lm.lora import CONFIG_DEFAULTS, train_model
from mlx_lm.tuner.callbacks import TrainingCallback
from mlx_lm.tuner.datasets import CacheDataset, ChatDataset
from mlx_lm.tuner.trainer import evaluate


class Recorder(TrainingCallback):
    def __init__(self, model, destination):
        self.model, self.destination = model, destination
        self.records, self.best = [], None
        self.started = time.perf_counter()

    def save(self, kind, values):
        self.records.append(
            {"kind": kind, "elapsed_seconds": round(time.perf_counter() - self.started, 3), **values}
        )
        (self.destination / "metrics.json").write_text(json.dumps(self.records, indent=2))
        print(json.dumps(self.records[-1]), flush=True)

    def on_train_loss_report(self, values):
        self.save("train", values)

    def on_val_loss_report(self, values):
        parameters = dict(tree_flatten(self.model.trainable_parameters()))
        if values["iteration"] == 0:
            mx.save_safetensors(str(self.destination / "initial.safetensors"), parameters)
        if values["iteration"] > 0 and (self.best is None or values["val_loss"] < self.best["val_loss"]):
            self.best = dict(values)
            mx.save_safetensors(str(self.destination / "best.safetensors"), parameters)
        self.save("valid", values)


def main():
    manifest = verify_frozen()
    config = json.loads((ROOT / "training/config.json").read_text())
    np.random.seed(config["seed"])
    audit = json.loads((ROOT / "reports/sft-data-audit.json").read_text())
    assert audit["training_config_sha256"] == sha(ROOT / "training/config.json")
    assert audit["dataset"] == manifest
    destination, best_path = ROOT / config["adapter_path"], ROOT / config["best_path"]
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Training run directory already exists; do not silently replace checkpoints")
    destination.mkdir(parents=True, exist_ok=True)
    model, tokenizer = load(
        ROOT / config["model"], tokenizer_config={"trust_remote_code": False}, trust_remote_code=False
    )
    tokenizer = NoThinkingTokenizer(tokenizer)
    training = ChatDataset(read_rows("train"), tokenizer, mask_prompt=True)
    validation = ChatDataset(read_rows("valid"), tokenizer, mask_prompt=True)
    args = {**CONFIG_DEFAULTS, **{k: v for k, v in config.items() if k in CONFIG_DEFAULTS}}
    args.update(
        train=True,
        model=str(ROOT / config["model"]),
        data=str(ROOT / config["data"]),
        adapter_path=str(destination),
        report_to=None,
        trust_remote_code=False,
    )
    recorder = Recorder(model, destination)
    (destination / "protocol.json").write_text(
        json.dumps(
            {
                "config": config,
                "config_sha256": sha(ROOT / "training/config.json"),
                "runner_sha256": sha(__file__),
                "common_sha256": sha(ROOT / "training/common.py"),
                "dataset": manifest,
                "mlx_lm_version": importlib.metadata.version("mlx-lm"),
                "mlx_version": importlib.metadata.version("mlx"),
                "tokenizer_thinking": False,
                "prompt_loss_masked": True,
            },
            indent=2,
        )
    )
    train_model(types.SimpleNamespace(**args), model, training, validation, recorder)
    final_loss = evaluate(
        model, CacheDataset(validation), batch_size=1, num_batches=-1, max_seq_length=config["max_seq_length"]
    )
    recorder.on_val_loss_report({"iteration": config["iters"], "val_loss": final_loss, "val_time": None})
    assert recorder.best is not None
    best_path.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(destination / "best.safetensors", best_path / "adapters.safetensors")
    shutil.copyfile(destination / "adapter_config.json", best_path / "adapter_config.json")
    initial = mx.load(str(destination / "initial.safetensors"))
    selected = mx.load(str(best_path / "adapters.safetensors"))
    delta = (
        sum(
            float(mx.sum((selected[k].astype(mx.float32) - initial[k].astype(mx.float32)) ** 2))
            for k in initial
        )
        ** 0.5
    )
    assert delta > 0, "Adapter tensor values must actually change"
    report = {
        "status": "trained",
        "config": config,
        "dataset": manifest,
        "config_sha256": sha(ROOT / "training/config.json"),
        "model_source": json.loads((ROOT / "data/models/qwen3-4b-mlx-source.json").read_text()),
        "best_checkpoint": recorder.best,
        "metrics": recorder.records,
        "initial_adapter_sha256": sha(destination / "initial.safetensors"),
        "selected_adapter_sha256": sha(best_path / "adapters.safetensors"),
        "adapter_tensor_count": len(selected),
        "trainable_parameters": sum(int(p.size) for p in selected.values()),
        "adapter_l2_change": delta,
        "elapsed_seconds": round(time.perf_counter() - recorder.started, 2),
        "peak_memory_bytes": mx.get_peak_memory(),
        "test_used_for_selection": False,
        "cloud_compute_used": False,
    }
    (ROOT / "reports/sft-training.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "status",
                    "best_checkpoint",
                    "trainable_parameters",
                    "adapter_l2_change",
                    "elapsed_seconds",
                    "peak_memory_bytes",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
