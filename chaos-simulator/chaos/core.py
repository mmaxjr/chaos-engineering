"""
Core do Chaos Engineering Simulator.

Define:
- MetricsSample / MetricsCollector: coleta de métricas do sistema (CPU, memória, disco, rede)
  antes, durante e depois de um experimento.
- ExperimentResult: resultado estruturado de uma execução.
- Experiment: classe base abstrata que todo experimento de caos deve implementar.
- ReportGenerator: gera relatórios Markdown no estilo "game day report".
"""

from __future__ import annotations

import json
import platform
import socket
import statistics
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psutil


# --------------------------------------------------------------------------- #
# Métricas
# --------------------------------------------------------------------------- #

@dataclass
class MetricsSample:
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    disk_percent: float
    net_bytes_sent: int
    net_bytes_recv: int
    load_avg: Optional[tuple] = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


class MetricsCollector:
    """Coleta métricas de sistema em intervalos regulares durante um experimento."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.samples: list[MetricsSample] = []
        self._running = False

    def sample_once(self) -> MetricsSample:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()
        try:
            load_avg = psutil.getloadavg()
        except (AttributeError, OSError):
            load_avg = None

        sample = MetricsSample(
            timestamp=time.time(),
            cpu_percent=psutil.cpu_percent(interval=None),
            memory_percent=vm.percent,
            memory_used_mb=vm.used / (1024 * 1024),
            disk_percent=disk.percent,
            net_bytes_sent=net.bytes_sent,
            net_bytes_recv=net.bytes_recv,
            load_avg=load_avg,
        )
        self.samples.append(sample)
        return sample

    def summary(self) -> dict:
        if not self.samples:
            return {}

        def stats(values: list[float]) -> dict:
            return {
                "min": round(min(values), 2),
                "max": round(max(values), 2),
                "avg": round(statistics.mean(values), 2),
            }

        return {
            "samples_collected": len(self.samples),
            "cpu_percent": stats([s.cpu_percent for s in self.samples]),
            "memory_percent": stats([s.memory_percent for s in self.samples]),
            "memory_used_mb": stats([s.memory_used_mb for s in self.samples]),
            "disk_percent": stats([s.disk_percent for s in self.samples]),
        }


# --------------------------------------------------------------------------- #
# Resultado do experimento
# --------------------------------------------------------------------------- #

@dataclass
class ExperimentResult:
    experiment_id: str
    name: str
    category: str
    hypothesis: str
    target: str
    parameters: dict
    started_at: str
    finished_at: str
    duration_seconds: float
    status: str  # "success" | "failed" | "aborted"
    steady_state_before: dict
    steady_state_after: dict
    metrics_summary: dict
    observations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    conclusion: str = ""
    hostname: str = field(default_factory=socket.gethostname)
    platform_info: str = field(default_factory=platform.platform)

    def to_dict(self) -> dict:
        return self.__dict__


# --------------------------------------------------------------------------- #
# Classe base de experimento
# --------------------------------------------------------------------------- #

class Experiment:
    """
    Classe base para todo experimento de chaos engineering.

    Ciclo de vida:
      1. check_prerequisites()  -> valida se o experimento pode rodar com segurança
      2. steady_state()         -> mede o estado "normal" do sistema/alvo antes do experimento
      3. inject()               -> aplica a falha (implementado pela subclasse)
      4. probe()                -> observa o comportamento durante a falha (opcional, subclasse)
      5. rollback()             -> reverte a falha, garantida via try/finally
      6. steady_state()         -> mede o estado depois, para comparação
      7. validate()             -> compara antes/depois e decide status + conclusão
    """

    name: str = "experimento_base"
    category: str = "generico"
    hypothesis: str = "O sistema deve permanecer estável durante a falha injetada."

    def __init__(
        self,
        target: str = "local",
        duration: float = 10.0,
        metrics_interval: float = 1.0,
        parameters: Optional[dict] = None,
        dry_run: bool = False,
    ):
        self.target = target
        self.duration = duration
        self.parameters = parameters or {}
        self.dry_run = dry_run
        self.metrics = MetricsCollector(interval=metrics_interval)
        self.observations: list[str] = []
        self.errors: list[str] = []
        self.experiment_id = f"{self.category}-{uuid.uuid4().hex[:8]}"

    # --- Hooks a serem sobrescritos pelas subclasses -------------------- #

    def check_prerequisites(self) -> None:
        """Levanta exceção se o experimento não puder rodar com segurança."""
        return None

    def steady_state(self) -> dict:
        """Retorna um dicionário descrevendo o estado 'normal' do sistema."""
        sample = self.metrics.sample_once()
        return sample.to_dict()

    def inject(self) -> None:
        raise NotImplementedError("Subclasses devem implementar inject()")

    def probe(self) -> None:
        """Chamado repetidamente durante a janela de duração do experimento."""
        self.metrics.sample_once()

    def rollback(self) -> None:
        raise NotImplementedError("Subclasses devem implementar rollback()")

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        """Retorna (status, conclusao). Subclasses podem sobrescrever para regras específicas."""
        return "success", (
            "Sistema retornou a um estado consistente com o baseline após a remoção da falha."
        )

    def observe(self, message: str) -> None:
        self.observations.append(message)

    # --- Execução --------------------------------------------------------- #

    def run(self) -> ExperimentResult:
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.time()
        status = "failed"
        conclusion = ""
        before = {}
        after = {}

        try:
            self.check_prerequisites()

            self.observe("Medindo steady-state antes do experimento.")
            before = self.steady_state()

            if self.dry_run:
                self.observe("Modo dry-run: falha não foi realmente injetada.")
                status, conclusion = "success", "Dry-run concluído sem injeção real de falha."
            else:
                self.observe(f"Injetando falha: {self.name} (alvo={self.target}).")
                try:
                    self.inject()
                    end_time = time.time() + self.duration
                    while time.time() < end_time:
                        self.probe()
                        time.sleep(min(self.metrics.interval, max(end_time - time.time(), 0)))
                finally:
                    self.observe("Executando rollback / limpeza.")
                    self.rollback()

                self.observe("Medindo steady-state depois do experimento.")
                after = self.steady_state()
                status, conclusion = self.validate(before, after)

        except Exception as exc:  # noqa: BLE001
            status = "aborted"
            self.errors.append(f"{type(exc).__name__}: {exc}")
            self.errors.append(traceback.format_exc())
            conclusion = "Experimento abortado devido a um erro. Ver seção de erros."

        finished_at = datetime.now(timezone.utc).isoformat()
        duration = round(time.time() - t0, 2)

        return ExperimentResult(
            experiment_id=self.experiment_id,
            name=self.name,
            category=self.category,
            hypothesis=self.hypothesis,
            target=self.target,
            parameters=self.parameters,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            status=status,
            steady_state_before=before,
            steady_state_after=after,
            metrics_summary=self.metrics.summary(),
            observations=self.observations,
            errors=self.errors,
            conclusion=conclusion,
        )


# --------------------------------------------------------------------------- #
# Geração de relatório Markdown
# --------------------------------------------------------------------------- #

class ReportGenerator:
    """Gera relatórios Markdown ('game day reports') a partir de um ExperimentResult."""

    STATUS_EMOJI = {
        "success": "✅",
        "failed": "❌",
        "aborted": "⚠️",
    }

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, result: ExperimentResult) -> Path:
        md = self._render(result)
        filename = f"{result.started_at[:19].replace(':', '-')}_{result.experiment_id}.md"
        path = self.output_dir / filename
        path.write_text(md, encoding="utf-8")

        json_path = self.output_dir / f"{result.started_at[:19].replace(':', '-')}_{result.experiment_id}.json"
        json_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

        return path

    def _render(self, r: ExperimentResult) -> str:
        emoji = self.STATUS_EMOJI.get(r.status, "❔")
        lines = []
        lines.append(f"# Relatório de Experimento de Chaos Engineering")
        lines.append("")
        lines.append(f"**Experimento:** {r.name}  ")
        lines.append(f"**ID:** `{r.experiment_id}`  ")
        lines.append(f"**Categoria:** {r.category}  ")
        lines.append(f"**Status:** {emoji} {r.status.upper()}  ")
        lines.append(f"**Alvo:** {r.target}  ")
        lines.append(f"**Host:** {r.hostname} ({r.platform_info})  ")
        lines.append("")
        lines.append("## Hipótese")
        lines.append("")
        lines.append(r.hypothesis)
        lines.append("")
        lines.append("## Parâmetros")
        lines.append("")
        if r.parameters:
            lines.append("| Parâmetro | Valor |")
            lines.append("|---|---|")
            for k, v in r.parameters.items():
                lines.append(f"| {k} | {v} |")
        else:
            lines.append("_Nenhum parâmetro adicional._")
        lines.append("")
        lines.append("## Linha do tempo")
        lines.append("")
        lines.append(f"- Início: `{r.started_at}`")
        lines.append(f"- Fim: `{r.finished_at}`")
        lines.append(f"- Duração: `{r.duration_seconds}s`")
        lines.append("")
        lines.append("## Observações")
        lines.append("")
        if r.observations:
            for obs in r.observations:
                lines.append(f"- {obs}")
        else:
            lines.append("_Sem observações registradas._")
        lines.append("")
        lines.append("## Steady-state (antes vs. depois)")
        lines.append("")
        lines.append("| Métrica | Antes | Depois |")
        lines.append("|---|---|---|")
        before = r.steady_state_before or {}
        after = r.steady_state_after or {}
        keys = sorted(set(before.keys()) | set(after.keys()) - {"timestamp"})
        for k in keys:
            if k == "timestamp":
                continue
            lines.append(f"| {k} | {before.get(k, '-')} | {after.get(k, '-')} |")
        lines.append("")
        lines.append("## Métricas durante o experimento")
        lines.append("")
        if r.metrics_summary:
            lines.append("| Métrica | Min | Média | Max |")
            lines.append("|---|---|---|---|")
            for metric, stat in r.metrics_summary.items():
                if isinstance(stat, dict):
                    lines.append(f"| {metric} | {stat.get('min')} | {stat.get('avg')} | {stat.get('max')} |")
            lines.append("")
            lines.append(f"_Amostras coletadas: {r.metrics_summary.get('samples_collected', 0)}_")
        else:
            lines.append("_Sem métricas coletadas (possivelmente dry-run)._")
        lines.append("")
        if r.errors:
            lines.append("## Erros")
            lines.append("")
            lines.append("```")
            for err in r.errors:
                lines.append(err)
            lines.append("```")
            lines.append("")
        lines.append("## Conclusão")
        lines.append("")
        lines.append(r.conclusion or "_Sem conclusão registrada._")
        lines.append("")
        return "\n".join(lines)
