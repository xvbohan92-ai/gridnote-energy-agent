"""Download a pinned public checkpoint into ignored project-local storage."""

import json
import os
import time

from common import ROOT, sha


def main():
    config = json.loads((ROOT / "training/config.json").read_text())
    destination = ROOT / config["model"]
    source_file = ROOT / "data/models/qwen3-4b-mlx-source.json"
    if source_file.exists():
        source = json.loads(source_file.read_text())
        if source["repo"] != config["model_repo"] or source["revision"] != config["model_revision"]:
            raise ValueError("Existing model snapshot has a different identity; use a new experiment path")
        assert all(sha(destination / item["path"]) == item["sha256"] for item in source["files"])
        print("Existing pinned model files verified; no download or metadata replacement.")
        return
    os.environ["HF_HOME"] = str(ROOT / "data/hf-cache")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import snapshot_download

    started = time.perf_counter()
    snapshot_download(
        repo_id=config["model_repo"],
        revision=config["model_revision"],
        local_dir=destination,
        cache_dir=ROOT / "data/hf-cache",
        token=False,
    )
    files = [
        {"path": p.name, "bytes": p.stat().st_size, "sha256": sha(p)}
        for p in sorted(destination.iterdir())
        if p.is_file()
    ]
    source_file.write_text(
        json.dumps(
            {
                "repo": config["model_repo"],
                "revision": config["model_revision"],
                "files": files,
                "download_seconds": time.perf_counter() - started,
            },
            indent=2,
        )
    )
    print(json.dumps({"files": len(files), "bytes": sum(f["bytes"] for f in files)}))


if __name__ == "__main__":
    main()
