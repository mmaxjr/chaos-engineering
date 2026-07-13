"""
CLI do Chaos Engineering Simulator.

Uso:
    python -m chaos.cli list
    python -m chaos.cli run process-kill --duration 5
    python -m chaos.cli run network-latency --delay-ms 200 --duration 10
    python -m chaos.cli gameday config/gameday_example.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import ReportGenerator
from .experiments import REGISTRY

DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"


def _build_experiment(name: str, args: argparse.Namespace):
    cls = REGISTRY.get(name)
    if not cls:
        print(f"Experimento desconhecido: {name}. Use 'list' para ver as opções.", file=sys.stderr)
        sys.exit(1)

    kwargs = {
        "duration": args.duration,
        "metrics_interval": args.interval,
        "dry_run": args.dry_run,
        "target": args.target,
    }

    if name == "process-kill":
        kwargs.update({"pid": args.pid, "process_name": args.process_name, "sig": args.signal})
    elif name == "process-crash-loop":
        kwargs.update({"crash_interval": args.crash_interval})
    elif name in ("network-latency", "packet-loss", "network-partition"):
        kwargs.update({"interface": args.interface, "delay_ms": args.delay_ms, "loss_pct": args.loss_pct})
    elif name == "cpu-stress":
        kwargs.update({"workers": args.workers})
    elif name == "memory-stress":
        kwargs.update({"target_mb": args.target_mb})
    elif name == "disk-fill":
        kwargs.update({"target_mb": args.target_mb, "path": args.path})

    return cls(**kwargs)


def cmd_list(_args) -> None:
    print("Experimentos disponíveis:\n")
    for key, cls in REGISTRY.items():
        print(f"  {key:22s} - {cls.name} ({cls.category})")


def cmd_run(args) -> None:
    experiment = _build_experiment(args.experiment, args)
    print(f"Executando '{experiment.name}' (target={experiment.target}, duração={experiment.duration}s)...")
    result = experiment.run()
    report_dir = Path(args.report_dir) if args.report_dir else DEFAULT_REPORT_DIR
    report = ReportGenerator(report_dir)
    path = report.generate(result)
    print(f"\nStatus: {result.status.upper()}")
    print(f"Conclusão: {result.conclusion}")
    print(f"Relatório salvo em: {path}")


def cmd_gameday(args) -> None:
    import yaml  # import tardio para não exigir a dependência se não usado

    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    report_dir = Path(args.report_dir) if args.report_dir else DEFAULT_REPORT_DIR
    report = ReportGenerator(report_dir)

    print(f"Iniciando game day: {config.get('name', config_path.stem)}")
    results = []
    for step in config.get("experiments", []):
        exp_name = step["type"]
        cls = REGISTRY[exp_name]
        params = {k: v for k, v in step.items() if k != "type"}
        experiment = cls(**params)
        print(f"\n-> Executando: {experiment.name}")
        result = experiment.run()
        path = report.generate(result)
        print(f"   status={result.status} relatorio={path.name}")
        results.append(result)

    print("\nResumo do game day:")
    for r in results:
        print(f"  [{r.status.upper():8s}] {r.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chaos Engineering Simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Lista experimentos disponíveis")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="Executa um único experimento")
    p_run.add_argument("experiment", choices=list(REGISTRY.keys()))
    p_run.add_argument("--target", default="local")
    p_run.add_argument("--duration", type=float, default=10.0)
    p_run.add_argument("--interval", type=float, default=1.0)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--report-dir", default=None)
    # process-kill
    p_run.add_argument("--pid", type=int, default=None)
    p_run.add_argument("--process-name", default=None)
    p_run.add_argument("--signal", default="SIGKILL")
    # process-crash-loop
    p_run.add_argument("--crash-interval", type=float, default=2.0)
    # network
    p_run.add_argument("--interface", default="lo")
    p_run.add_argument("--delay-ms", type=float, default=100)
    p_run.add_argument("--loss-pct", type=float, default=10)
    # cpu
    p_run.add_argument("--workers", type=int, default=None)
    # memory / disk
    p_run.add_argument("--target-mb", type=int, default=256)
    p_run.add_argument("--path", default=None)
    p_run.set_defaults(func=cmd_run)

    p_gameday = sub.add_parser("gameday", help="Executa uma suíte de experimentos a partir de um YAML")
    p_gameday.add_argument("config")
    p_gameday.add_argument("--report-dir", default=None)
    p_gameday.set_defaults(func=cmd_gameday)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
