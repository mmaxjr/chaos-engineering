"""
Experimentos de falha de processo/servico.

Por seguranca, estes experimentos NUNCA matam processos arbitrarios do sistema
a menos que um PID/nome seja explicitamente informado pelo usuario. O modo padrao
sobe um "processo vitima" proprio (um pequeno servidor de demonstracao) para que
o experimento seja seguro de rodar em qualquer maquina.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from ..core import Experiment

VICTIM_SCRIPT = Path(__file__).parent / "_victim_service.py"


def _spawn_victim() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(VICTIM_SCRIPT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _is_alive(pid: int | None) -> bool:
    """Considera um processo 'morto' se ele nao existe ou esta em estado zumbi
    (ja recebeu o sinal mas ainda nao foi colhido/reaped pelo processo pai)."""
    if not pid or not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class ProcessKillExperiment(Experiment):
    """Mata um processo (por padrao, um processo vitima de demonstracao) e observa a recuperacao."""

    name = "Falha abrupta de processo"
    category = "process"
    hypothesis = (
        "O servico supervisor/orquestrador deve detectar o encerramento do processo "
        "e reinicia-lo (ou o sistema deve degradar de forma controlada) dentro da janela do experimento."
    )

    def __init__(self, pid: int | None = None, process_name: str | None = None,
                 sig: str = "SIGKILL", **kwargs):
        super().__init__(**kwargs)
        self.pid = pid
        self.process_name = process_name
        self.sig_name = sig
        self._victim_proc: subprocess.Popen | None = None
        self._spawned_victim = pid is None and process_name is None
        self.parameters.update({
            "pid": pid,
            "process_name": process_name,
            "signal": sig,
            "spawned_demo_victim": self._spawned_victim,
        })

    def check_prerequisites(self) -> None:
        if not self._spawned_victim and self.pid is not None:
            if not psutil.pid_exists(self.pid):
                raise RuntimeError(f"PID {self.pid} nao existe.")
        if not hasattr(signal, self.sig_name):
            raise RuntimeError(f"Sinal desconhecido: {self.sig_name}")

    def _reap(self) -> None:
        """Para o processo vitima gerado via subprocess.Popen, poll() precisa ser chamado
        para que o SO libere (reap) o processo apos ele morrer, evitando que fique como zumbi."""
        if self._victim_proc:
            self._victim_proc.poll()

    def steady_state(self) -> dict:
        base = super().steady_state()
        if self._spawned_victim and self._victim_proc is None:
            self._victim_proc = _spawn_victim()
            time.sleep(0.5)  # da tempo do processo subir
        self._reap()
        target_pid = self._resolve_pid()
        base["target_process_alive"] = _is_alive(target_pid)
        base["target_pid"] = target_pid
        return base

    def _resolve_pid(self) -> int | None:
        if self._spawned_victim:
            return self._victim_proc.pid if self._victim_proc else None
        if self.pid:
            return self.pid
        if self.process_name:
            for proc in psutil.process_iter(["pid", "name"]):
                if proc.info["name"] == self.process_name:
                    return proc.info["pid"]
        return None

    def inject(self) -> None:
        target_pid = self._resolve_pid()
        if not target_pid:
            raise RuntimeError("Nao foi possivel resolver o PID alvo.")
        sig = getattr(signal, self.sig_name)
        self.observe(f"Enviando {self.sig_name} para PID {target_pid}.")
        os.kill(target_pid, sig)

    def probe(self) -> None:
        super().probe()
        self._reap()
        target_pid = self._resolve_pid()
        alive = _is_alive(target_pid)
        self.observe(f"t={round(time.time(),1)} processo alvo vivo={alive}")

    def rollback(self) -> None:
        if self._spawned_victim and self._victim_proc:
            if self._victim_proc.poll() is None:
                self._victim_proc.terminate()
                self._victim_proc.wait(timeout=2)
            self.observe("Processo vitima de demonstracao finalizado (rollback).")

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        was_alive = before.get("target_process_alive", False)
        is_alive_after = after.get("target_process_alive", False)
        if was_alive and not is_alive_after:
            return (
                "success",
                "O processo alvo foi encerrado com sucesso pelo experimento e nao foi "
                "automaticamente reiniciado (esperado, pois nao ha supervisor configurado neste demo). "
                "Em producao, valide que seu orquestrador -- systemd, Docker restart policy, ou "
                "Kubernetes liveness/readiness probes -- restaura o servico automaticamente."
            )
        return "failed", "O processo alvo continuou vivo apos a injecao de falha; verifique o sinal utilizado."


class ProcessCrashLoopExperiment(Experiment):
    """Simula um crash-loop: mata e relanca o processo vitima repetidamente durante a duracao."""

    name = "Crash-loop de processo"
    category = "process"
    hypothesis = (
        "O sistema deve tolerar reinicios repetidos do servico sem acumular vazamento de "
        "recursos (memoria/handles) nem degradacao persistente."
    )

    def __init__(self, crash_interval: float = 2.0, **kwargs):
        super().__init__(**kwargs)
        self.crash_interval = crash_interval
        self._victim_proc: subprocess.Popen | None = None
        self.restart_count = 0
        self.parameters.update({"crash_interval_s": crash_interval})

    def steady_state(self) -> dict:
        base = super().steady_state()
        if self._victim_proc is None:
            self._victim_proc = _spawn_victim()
            time.sleep(0.5)
        base["restart_count"] = self.restart_count
        return base

    def inject(self) -> None:
        self.observe("Iniciando ciclo de crash-loop.")

    def probe(self) -> None:
        super().probe()
        if self._victim_proc and self._victim_proc.poll() is None:
            self._victim_proc.kill()
            self._victim_proc.wait()
            self.restart_count += 1
            self._victim_proc = _spawn_victim()
            self.observe(f"Crash #{self.restart_count}: processo morto e relancado.")
        time.sleep(self.crash_interval)

    def rollback(self) -> None:
        if self._victim_proc and self._victim_proc.poll() is None:
            self._victim_proc.terminate()
        self.observe(f"Rollback concluido. Total de restarts durante o experimento: {self.restart_count}.")

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        after["restart_count"] = self.restart_count
        if self.restart_count > 0:
            return (
                "success",
                f"Crash-loop executado com {self.restart_count} reinicios. "
                "Recomenda-se correlacionar este resultado com metricas de memoria/handles do "
                "processo real em producao para detectar vazamentos acumulados entre reinicios."
            )
        return "failed", "Nenhum ciclo de crash foi executado; verifique a duracao do experimento."
