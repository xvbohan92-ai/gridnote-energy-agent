import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_rows(split):
    return [json.loads(line) for line in (ROOT / f"training/datasets/{split}.jsonl").read_text().splitlines()]


def verify_frozen():
    manifest = json.loads((ROOT / "training/datasets/manifest.json").read_text())
    for split, record in manifest["splits"].items():
        if sha(ROOT / f"training/datasets/{split}.jsonl") != record["sha256"]:
            raise ValueError(f"Frozen {split} data changed")
    if sha(ROOT / "training/prepare_data.py") != manifest["builder_sha256"]:
        raise ValueError("Dataset builder changed after freezing")
    if sha(ROOT / "energy_agent/sql_sft.py") != manifest["prompt_module_sha256"]:
        raise ValueError("Planner prompt changed after freezing")
    return manifest


class NoThinkingTokenizer:
    """Explicit template option for both masked SFT encoding and generation."""

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def __getattr__(self, name):
        return getattr(self.tokenizer, name)

    def apply_chat_template(self, *args, **kwargs):
        kwargs["enable_thinking"] = False
        return self.tokenizer.apply_chat_template(*args, **kwargs)
