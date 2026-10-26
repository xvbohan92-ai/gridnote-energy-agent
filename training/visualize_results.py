"""Source-backed static figures for the completed, frozen local QLoRA experiment."""

import csv
import json

from common import ROOT, sha


def main():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    evaluated = json.loads((ROOT / "reports/sft-evaluation.json").read_text())
    trained = json.loads((ROOT / "reports/sft-training.json").read_text())
    destination = ROOT / "docs/figures"
    destination.mkdir(exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "svg.fonttype": "none",
        }
    )
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.8))
    colors = ["#9aa6a0", "#347459"]
    phases = ["base", "qlora"]
    correct = [evaluated["summary"][p]["execution_correct"] for p in phases]
    axes[0, 0].bar([0, 1], correct, color=colors, width=0.55)
    axes[0, 0].set(
        xticks=[0, 1],
        xticklabels=["Same MLX base", "QLoRA adapter"],
        ylim=(0, 36),
        ylabel="Execution-correct cases / 32",
        title="A. Frozen contract execution check",
    )
    for index, value in enumerate(correct):
        axes[0, 0].text(index, value + 0.8, f"{value}/32  ({value / 32:.1%})", ha="center", fontsize=11)
    delta = (correct[1] - correct[0]) / 32 * 100
    axes[0, 0].text(0.5, 33, f"+{delta:.1f} percentage points", ha="center", color="#347459")
    for index, phase in enumerate(phases):
        values = [evaluated["by_mode"][phase][mode] for mode in ("direct", "repair")]
        positions = [x + (index - 0.5) * 0.3 for x in (0, 1)]
        accuracy = [v["execution_accuracy"] * 100 for v in values]
        axes[0, 1].bar(positions, accuracy, width=0.28, color=colors[index], label=phase)
        for x, y, value in zip(positions, accuracy, values, strict=True):
            axes[0, 1].text(x, y + 2, f"{value['execution_correct']}/{value['cases']}", ha="center")
    axes[0, 1].set(
        xticks=[0, 1],
        xticklabels=["Direct generation", "Supplied repair"],
        ylim=(0, 114),
        ylabel="Execution correct (%)",
        title="B. Separate the two task types",
    )
    axes[0, 1].legend(frameon=False, loc="upper left")
    for kind, color, label in (
        ("train", "#9aa6a0", "Training, 10-step mean"),
        ("valid", "#347459", "Full validation set"),
    ):
        values = [r for r in trained["metrics"] if r["kind"] == kind]
        axes[1, 0].plot(
            [r["iteration"] for r in values],
            [r["train_loss"] if kind == "train" else r["val_loss"] for r in values],
            marker="o",
            markersize=3,
            color=color,
            label=label,
        )
    best = trained["best_checkpoint"]
    axes[1, 0].scatter(
        [best["iteration"]],
        [best["val_loss"]],
        marker="*",
        color="#b67e45",
        s=140,
        zorder=4,
        label=f"Selected step {best['iteration']}",
    )
    axes[1, 0].set(
        yscale="log",
        xlabel="Optimizer steps",
        ylabel="Completion-only CE loss (log)",
        title="C. Validation-only checkpoint selection",
    )
    axes[1, 0].legend(frameon=False, fontsize=8)
    keys = ["improved", "regressed", "both_correct", "both_wrong"]
    counts = [evaluated["paired"][key] for key in keys]
    axes[1, 1].barh(range(4), counts, color=["#347459", "#b67e45", "#859d8d", "#a6aaa7"], height=0.6)
    axes[1, 1].set(
        yticks=range(4),
        yticklabels=["Improved", "Regressed", "Both correct", "Both wrong"],
        xlabel="Paired cases",
        xlim=(0, max(counts) + 4),
        title="D. Retain failures and regressions",
    )
    axes[1, 1].invert_yaxis()
    for index, value in enumerate(counts):
        axes[1, 1].text(value + 0.3, index, str(value), va="center")
    for ax in axes.flat:
        ax.set_axisbelow(True)
        ax.grid(axis="y" if ax != axes[1, 1] else "x", alpha=0.14)
    fig.suptitle("Qwen3-4B energy SQL | actual local 4-bit QLoRA pilot", fontsize=16, y=0.985)
    fig.text(
        0.05,
        0.052,
        "232 generated training instructions; 32 test cases from 8 held-out condition groups. "
        "Correct finite task contracts supplied; one schema.",
        fontsize=9,
    )
    fig.text(
        0.05,
        0.029,
        "Same model, prompt and greedy decoding; no calculator fallback or retry. "
        "Repair faults are synthetic. Not general text-to-SQL or Agent accuracy.",
        fontsize=9,
    )
    fig.text(
        0.05,
        0.008,
        "Sources: reports/sft-training.json and reports/sft-evaluation.json. "
        "A separate raw EIA CSV oracle scores returned values, fields, row counts and order.",
        fontsize=8,
        color="#657169",
    )
    fig.tight_layout(rect=(0.02, 0.08, 0.99, 0.95), h_pad=3, w_pad=3)
    for extension in ("png", "svg"):
        path = destination / f"sft-evaluation.{extension}"
        fig.savefig(path, dpi=160)
        if extension == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
    plt.close(fig)
    fields = [
        "id",
        "phase",
        "mode",
        "format_valid",
        "sql_guard_pass",
        "executed",
        "execution_correct",
        "chart_valid",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "failure",
    ]
    with (destination / "sft-case-results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(evaluated["records"])
    values = {
        "training_report_sha256": sha(ROOT / "reports/sft-training.json"),
        "evaluation_report_sha256": sha(ROOT / "reports/sft-evaluation.json"),
        "case_csv_sha256": sha(destination / "sft-case-results.csv"),
        "summary": evaluated["summary"],
        "by_mode": evaluated["by_mode"],
        "paired": evaluated["paired"],
        "dataset": evaluated["dataset"],
        "selected_checkpoint": best,
        "limitations": evaluated["limitations"],
    }
    (destination / "sft-chart-values.json").write_text(json.dumps(values, indent=2) + "\n")
    print(
        json.dumps(
            {
                "figure": "docs/figures/sft-evaluation.png",
                "correct": correct,
                "delta_percentage_points": delta,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
