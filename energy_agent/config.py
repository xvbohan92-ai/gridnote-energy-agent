import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    planner: str = "demo"
    model: str = "gpt-4.1-mini"
    max_retries: int = 2
    max_rows: int = 500
    query_timeout: float = 2.0

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        path = Path(os.getenv("ENERGY_DB_PATH", "data/energy.sqlite"))
        return cls(
            db_path=path if path.is_absolute() else ROOT / path,
            planner=os.getenv("ENERGY_PLANNER", "demo"),
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )
