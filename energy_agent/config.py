import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def model_for(planner):
    if planner == "mlx-sql":
        return os.getenv("MLX_MODEL_LABEL", "Qwen3-4B QLoRA")
    if planner == "ollama-tools":
        return os.getenv("OLLAMA_MODEL", "qwen3:4b")
    return os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    planner: str = "demo"
    model: str = "gpt-4.1-mini"
    max_retries: int = 2
    max_rows: int = 500
    query_timeout: float = 2.0
    max_model_calls: int = 3
    max_tool_calls: int = 12
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    mlx_base_url: str = "http://127.0.0.1:8768"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        path = Path(os.getenv("ENERGY_DB_PATH", "data/energy.sqlite"))
        planner = os.getenv("ENERGY_PLANNER", "demo")
        return cls(
            db_path=path if path.is_absolute() else ROOT / path,
            planner=planner,
            model=model_for(planner),
            max_model_calls=max(1, min(12, int(os.getenv("ENERGY_MAX_MODEL_CALLS", "3")))),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            mlx_base_url=os.getenv("MLX_BASE_URL", "http://127.0.0.1:8768"),
        )
