import argparse
import json
from pathlib import Path

import uvicorn

from .agent import ask
from .config import Settings
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
    ingest = sub.add_parser("import-csv", help="Validate CSV and atomically replace the local dataset")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--source", required=True, help="Honest provenance label, e.g. PJM Data Miner export")
    ingest.add_argument(
        "--replace", action="store_true", help="Explicitly allow replacement of an existing database"
    )
    bench = sub.add_parser("evaluate", help="Run isolated synthetic smoke benchmark")
    bench.add_argument("--planner", choices=("demo", "openai"), default=None)
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
        elif args.command == "evaluate":
            report = evaluate(
                planner=args.planner or settings.planner, model=settings.model, output=args.output
            )
            output = {
                "report": str(args.output.resolve()),
                **report["summary"],
                "limitations": report["limitations"],
            }
        else:
            create_demo(settings.db_path)
            if args.command == "ask":
                output = ask(
                    settings,
                    AskRequest(
                        question=args.question, region=args.region, start_date=args.start, end_date=args.end
                    ),
                    mode=args.mode,
                )
            else:
                output = dataset_context(settings.db_path)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (DataError, PlanningError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
