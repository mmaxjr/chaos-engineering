"""
Experimentos de exaustao de recursos: CPU, memoria e disco.

Todos os experimentos desta categoria tem limites de seguranca embutidos
(percentual maximo de memoria, tamanho maximo de arquivo em disco, etc.)
para evitar derrubar a maquina onde o simulador esta rodando.
"""

from __future__ import annotations

import gc
import multiprocessing
import os
import tempfile
import time
from pathlib import Path

import psutil

from ..core import Experiment

# Limites de seguranca (podem ser ajustados via parameters, mas nunca ultrapassados)
MAX_MEMORY_PERCENT = 85
MAX_DISK_FILL_MB = 2048


def _cpu_burner(stop_event):
    while not stop_event.is_set():
        pass


class CPUStressExperiment(Experiment):
    name = "Estresse de CPU"
    category = "resource"
    hypothesis = (
        "O sistema deve manter tempos de resposta aceitaveis (ou aplicar throttling/"
        "autoscaling) quando a utilizacao de CPU se aproxima da saturacao."
    )

    def __init__(self, workers: int | None = None, **kwargs):
        super().__init__(**kwargs)
        self.workers = workers or max(1, multiprocessing.cpu_count() - 1)
        self._procs: list[multiprocessing.Process] = []
        self._stop_event = multiprocessing.Event()
        self.parameters.update({"workers": self.workers})

    def check_prerequisites(self) -> None:
        if self.workers > multiprocessing.cpu_count():
            raise RuntimeError("Numero de workers excede os nucleos de CPU disponiveis.")

    def inject(self) -> None:
        self._stop_event.clear()
        for _ in range(self.workers):
            p = multiprocessing.Process(target=_cpu_burner, args=(self._stop_event,), daemon=True)
            p.start()
            self._procs.append(p)
        self.observe(f"{self.workers} processo(s) de estresse de CPU iniciados.")

    def rollback(self) -> None:
        self._stop_event.set()
        for p in self._procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
        self._procs.clear()
        self.observe("Processos de estresse de CPU finalizados.")

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        peak = self.metrics.summary().get("cpu_percent", {}).get("max", 0)
        return (
            "success",
            f"Pico de CPU observado durante o experimento: {peak}%. "
            "Compare este valor com os limites de autoscaling/alerta configurados no seu ambiente."
        )


class MemoryStressExperiment(Experiment):
    name = "Estresse de memoria"
    category = "resource"
    hypothesis = (
        "O sistema deve liberar memoria adequadamente apos o pico de consumo, e nao deve "
        "sofrer OOM-kill de processos criticos dentro do limite de seguranca definido."
    )

    def __init__(self, target_mb: int = 256, **kwargs):
        super().__init__(**kwargs)
        self.target_mb = target_mb
        self._blocks: list[bytearray] = []
        self.parameters.update({"target_mb": target_mb})

    def check_prerequisites(self) -> None:
        vm = psutil.virtual_memory()
        projected_percent = vm.percent + (self.target_mb / (vm.total / (1024 * 1024))) * 100
        if projected_percent > MAX_MEMORY_PERCENT:
            raise RuntimeError(
                f"Alocacao de {self.target_mb}MB projetaria uso de memoria em "
                f"{projected_percent:.1f}%, acima do limite de seguranca de {MAX_MEMORY_PERCENT}%."
            )

    def inject(self) -> None:
        chunk_mb = 16
        chunks = max(1, self.target_mb // chunk_mb)
        for _ in range(chunks):
            self._blocks.append(bytearray(chunk_mb * 1024 * 1024))
        self.observe(f"~{chunks * chunk_mb}MB alocados em memoria.")

    def rollback(self) -> None:
        self._blocks.clear()
        gc.collect()
        time.sleep(0.5)  # da tempo do alocador/SO refletir a liberacao de memoria
        self.observe("Memoria alocada liberada (rollback + gc.collect()).")

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        before_mb = before.get("memory_used_mb", 0)
        after_mb = after.get("memory_used_mb", 0)
        delta = after_mb - before_mb
        if delta < self.target_mb * 0.9:
            return "success", (
                f"Memoria liberada corretamente apos o rollback (delta residual: {delta:.1f}MB "
                f"de {self.target_mb}MB alocados)."
            )
        return "failed", (
            f"Memoria nao foi totalmente liberada apos o rollback (delta residual: {delta:.1f}MB "
            f"de {self.target_mb}MB alocados). Possivel indicio de vazamento ou pressao de memoria "
            "do sistema no momento do teste."
        )


class DiskFillExperiment(Experiment):
    name = "Preenchimento de disco"
    category = "resource"
    hypothesis = (
        "Aplicacoes devem lidar com erros de 'disco cheio' de forma graciosa (retries, "
        "alertas, limpeza automatica de logs/tmp) em vez de travar ou corromper dados."
    )

    def __init__(self, target_mb: int = 512, path: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.target_mb = min(target_mb, MAX_DISK_FILL_MB)
        self.dir_path = Path(path) if path else Path(tempfile.gettempdir())
        self._file_path: Path | None = None
        self.parameters.update({"target_mb": self.target_mb, "path": str(self.dir_path)})

    def check_prerequisites(self) -> None:
        usage = psutil.disk_usage(str(self.dir_path))
        free_mb = usage.free / (1024 * 1024)
        if self.target_mb > free_mb * 0.8:
            raise RuntimeError(
                f"Alvo de {self.target_mb}MB excede 80% do espaco livre disponivel "
                f"({free_mb:.0f}MB) em {self.dir_path}."
            )

    def inject(self) -> None:
        self._file_path = self.dir_path / f"chaos_disk_fill_{self.experiment_id}.tmp"
        with open(self._file_path, "wb") as f:
            f.seek(self.target_mb * 1024 * 1024 - 1)
            f.write(b"\0")
        self.observe(f"Arquivo de {self.target_mb}MB criado em {self._file_path}.")

    def rollback(self) -> None:
        if self._file_path and self._file_path.exists():
            os.remove(self._file_path)
            self.observe(f"Arquivo temporario removido: {self._file_path}.")

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        before_disk = before.get("disk_percent", 0)
        after_disk = after.get("disk_percent", 0)
        if after_disk <= before_disk + 1:
            return "success", (
                f"Espaco em disco liberado corretamente apos o rollback "
                f"({before_disk}% -> pico -> {after_disk}%)."
            )
        return "failed", "Espaco em disco nao retornou ao nivel baseline apos o rollback."
