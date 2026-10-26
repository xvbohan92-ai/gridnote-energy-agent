import argparse
import json
from dataclasses import replace
from pathlib import Path

import uvicorn

from .agent import ask
from .config import Settings, model_for
from .data import DataError, create_demo, dataset_context, import_csv
from .evaluation import evaluate
from .models import AskRequest
from .planner import PlanningError


def main():
    parser = argparse.ArgumentParser(description="Energy Data Analysis Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Create synthetic data if database does not exist")
    sub.add_parser("inspect", help="Inspect current dataset and quality")
    serve = sub.add_parser("serve", help="Run local web application")
    serve.add_argument("--port", type=int, default=8000)
    query = sub.add_parser("ask", help="Ask a question, return inspectable JSON")
    query.add_argument("question")
    query.add_argument("--region")
    query.add_argument("--start")
    query.add_argument("--end")
    query.add_argument("--mode", choices=("baseline", "agent", "domain"), default="domain")
    query.add_argument("--planner", choices=("demo", "openai", "openai-tools", "ollama-tools", "mlx-sql"))
    ingest = sub.add_parser("import-csv", help="Validate CSV and atomically replace the local dataset")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--source", required=True, help="Honest provenance label, e.g. PJM Data Miner export")
    ingest.add_argument(
        "--replace", action="store_true", help="Explicitly allow replacement of an existing database"
    )
    eia = sub.add_parser("import-eia", help="Import a real UTC year of EIA-930 PJM demand")
    eia.add_argument("--year", type=int, default=2024)
    eia.add_argument("--sources", type=Path, default=Path("data/sources"))
    eia.add_argument("--download", action="store_true", help="Download public EIA snapshots if absent")
    eia.add_argument("--replace", action="store_true")
    eia.add_argument("--report", type=Path, default=Path("reports/eia-provenance.json"))
    real = sub.add_parser("validate-real", help="Check real-source integrity and the rule contract pilot")
    real.add_argument("--sources", type=Path, default=Path("data/sources"))
    real.add_argument("--output", type=Path, default=Path("reports/real-data-validation.json"))
    real.add_argument("--figures", type=Path)
    smoke = sub.add_parser("verify-model", help="Explicitly run one bounded tool-agent smoke check")
    smoke.add_argument("--provider", choices=("openai", "ollama", "mlx"))
    smoke.add_argument("--max-requests", type=int, choices=range(1, 4), default=3)
    smoke.add_argument("--output", type=Path, default=Path("reports/model-smoke.json"))
    bench = sub.add_parser("evaluate", help="Run isolated synthetic smoke benchmark")
    # Batch benchmarks must never inherit a paid tool-loop mode from .env.
    bench.add_argument("--planner", choices=("demo", "openai"), default="demo")
    bench.add_argument("--output", type=Path, default=Path("reports/evaluation.json"))
    figures = sub.add_parser(
        "visualize", help="Export verified project evidence as PNG/SVG and source tables"
    )
    figures.add_argument("--report", type=Path, default=Path("reports/evaluation.json"))
    figures.add_argument("--output", type=Path, default=Path("docs/figures"))
    figures.add_argument("--tests-xml", type=Path, default=Path("reports/test-results.xml"))
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        if args.command == "serve":
            from .api import create_app

            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
            return
        if args.command == "visualize":
            from .portfolio import export_portfolio

            output = export_portfolio(args.report, args.output, args.tests_xml)
        elif args.command == "import-csv":
            if settings.db_path.exists() and not args.replace:
                parser.error("数据库已存在；检查 CSV 后加 --replace，或通过 ENERGY_DB_PATH 指定新数据库。")
            import_csv(args.path, settings.db_path, source_label=args.source)
            output = dataset_context(settings.db_path)
        elif args.command == "import-eia":
            from .eia import download_sources, import_eia

            if settings.db_path.exists() and not args.replace:
                parser.error("数据库已存在；请换用 ENERGY_DB_PATH 或显式指定 --replace。")
            if args.download:
                download_sources(args.year, args.sources)
            output = import_eia(args.sources, settings.db_path, year=args.year, report=args.report)
        elif args.command == "validate-real":
            from .real_validation import validate_real

            report = validate_real(settings, args.sources, args.output, figures=args.figures)
            output = {
                "report": str(args.output),
                "summary": report["summary"],
                "row_integrity": report["row_integrity"],
                "model_calls": report["model_calls"],
                "limitations": report["limitations"],
            }
        elif args.command == "verify-model":
            provider = args.provider or (
                "mlx"
                if settings.planner == "mlx-sql"
                else "ollama"
                if settings.planner == "ollama-tools"
                else "openai"
            )
            selected = {"mlx": "mlx-sql", "ollama": "ollama-tools", "openai": "openai-tools"}[provider]
            configured = replace(
                settings,
                planner=selected,
                model=model_for(selected),
                max_model_calls=args.max_requests,
                max_retries=args.max_requests - 1,
            )
            response = ask(configured, AskRequest(question="最近7天的每日负荷趋势", region="PJM"))
            output = {
                "kind": "one-task-real-model-smoke",
                "request_budget": args.max_requests,
                "limitations": "Single-task smoke check, not an accuracy benchmark; no training is run here.",
                "response": response,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        elif args.command == "evaluate":
            report = evaluate(planner=args.planner, model=model_for("openai"), output=args.output)
            output = {
                "report": str(args.output.resolve()),
                **report["summary"],
                "limitations": report["limitations"],
            }
        else:
            create_demo(settings.db_path)
            if args.command == "ask":
                output = ask(
                    replace(
                        settings,
                        planner=args.planner,
                        model=model_for(args.planner),
                    )
                    if args.planner
                    else settings,
                    AskRequest(
                        question=args.question, region=args.region, start_date=args.start, end_date=args.end
                    ),
                    mode=args.mode,
                )
            else:
                output = dataset_context(settings.db_path)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "validate-real" and (
            not report["row_integrity"] or not all(r["correct"] for r in report["records"])
        ):
            parser.exit(1)
        if args.command == "verify-model" and response["outcome"] != "success":
            parser.exit(1)
    except (DataError, PlanningError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
