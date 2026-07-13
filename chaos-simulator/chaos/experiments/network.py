"""
Experimentos de latência, perda de pacotes e partição de rede.

Modo "real": usa `tc qdisc ... netem` (Linux, requer root/CAP_NET_ADMIN e o pacote
`iproute2`) para injetar falhas de rede de verdade na interface indicada.

Modo "simulado" (fallback automático): quando `tc` não está disponível ou a
operação falha por falta de privilégio, o experimento sobe um servidor/cliente
TCP local de eco e injeta a latência/perda artificialmente na camada de
aplicação, para que o impacto ainda possa ser medido e documentado com
segurança em qualquer máquina (inclusive dentro de contêineres sem
CAP_NET_ADMIN).
"""

from __future__ import annotations

import random
import shutil
import socket
import subprocess
import threading
import time

from ..core import Experiment


def _tc_available() -> bool:
    return shutil.which("tc") is not None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


class _EchoServer:
    """Servidor de eco TCP local usado no modo simulado para medir RTT."""

    def __init__(self, host="127.0.0.1", port=0, extra_latency_ms=0.0, loss_pct=0.0):
        self.host = host
        self.port = port
        self.extra_latency_ms = extra_latency_ms
        self.loss_pct = loss_pct
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.5)
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    data = conn.recv(1024)
                    if not data:
                        continue
                    if random.random() * 100 < self.loss_pct:
                        continue  # simula pacote perdido: não responde
                    if self.extra_latency_ms:
                        time.sleep(self.extra_latency_ms / 1000.0)
                    conn.sendall(data)
                except (socket.timeout, ConnectionError):
                    pass

    def stop(self):
        self._running = False
        self._thread.join(timeout=2)
        self._sock.close()


def _measure_rtt(host: str, port: int, attempts: int = 5, timeout: float = 2.0) -> list[float]:
    rtts = []
    for _ in range(attempts):
        try:
            t0 = time.time()
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.sendall(b"ping")
                s.settimeout(timeout)
                data = s.recv(1024)
                if data:
                    rtts.append((time.time() - t0) * 1000)
        except (socket.timeout, ConnectionError, OSError):
            rtts.append(None)  # perda de pacote / timeout
    return rtts


class _BaseNetworkExperiment(Experiment):
    category = "network"

    def __init__(self, interface: str = "lo", delay_ms: float = 0, loss_pct: float = 0,
                 target_ip: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.interface = interface
        self.delay_ms = delay_ms
        self.loss_pct = loss_pct
        self.target_ip = target_ip
        self.real_mode = _tc_available()
        self._server: _EchoServer | None = None
        self._tc_applied = False
        self.parameters.update({
            "interface": interface,
            "delay_ms": delay_ms,
            "loss_pct": loss_pct,
            "mode": "real (tc/netem)" if self.real_mode else "simulado (aplicação)",
        })

    def steady_state(self) -> dict:
        base = super().steady_state()
        if self._server is None:
            self._server = _EchoServer(extra_latency_ms=0, loss_pct=0)
            self._server.start()
        rtts = _measure_rtt("127.0.0.1", self._server.port)
        valid = [r for r in rtts if r is not None]
        base["rtt_ms_avg"] = round(sum(valid) / len(valid), 2) if valid else None
        base["packet_loss_observed_pct"] = round(100 * (len(rtts) - len(valid)) / len(rtts), 1)
        return base

    def _apply_tc(self, netem_args: list[str]) -> bool:
        try:
            _run(["tc", "qdisc", "del", "dev", self.interface, "root"])  # limpa estado anterior
            result = _run(["tc", "qdisc", "add", "dev", self.interface, "root", "netem", *netem_args])
            if result.returncode == 0:
                self._tc_applied = True
                return True
            self.observe(f"tc falhou ({result.stderr.strip()}); usando modo simulado.")
            return False
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            self.observe(f"tc indisponível ({exc}); usando modo simulado.")
            return False

    def _clear_tc(self):
        if self._tc_applied:
            _run(["tc", "qdisc", "del", "dev", self.interface, "root"])
            self._tc_applied = False

    def probe(self) -> None:
        super().probe()
        if self._server:
            rtts = _measure_rtt("127.0.0.1", self._server.port, attempts=3)
            valid = [r for r in rtts if r is not None]
            avg = round(sum(valid) / len(valid), 2) if valid else None
            self.observe(f"t={round(time.time(),1)} rtt_avg_ms={avg} amostras_perdidas={rtts.count(None)}/{len(rtts)}")

    def rollback(self) -> None:
        self._clear_tc()
        if self._server:
            self._server.stop()
            self._server = None

    def validate(self, before: dict, after: dict) -> tuple[str, str]:
        before_rtt = before.get("rtt_ms_avg") or 0
        after_rtt = after.get("rtt_ms_avg") or 0
        mode = self.parameters["mode"]
        return (
            "success",
            f"Falha de rede aplicada em modo {mode}. RTT baseline={before_rtt}ms, "
            f"RTT pós-rollback={after_rtt}ms (deve retornar próximo ao baseline). "
            "Consulte 'Métricas durante o experimento' para o impacto observado durante a janela de falha."
        )


class NetworkLatencyExperiment(_BaseNetworkExperiment):
    name = "Injeção de latência de rede"
    hypothesis = (
        "O serviço deve continuar respondendo dentro de um SLA aceitável mesmo com latência "
        "de rede adicional, e a latência deve desaparecer completamente após o rollback."
    )

    def inject(self) -> None:
        if self.real_mode:
            applied = self._apply_tc([f"delay", f"{self.delay_ms}ms"])
            if applied:
                self.observe(f"tc netem delay={self.delay_ms}ms aplicado em {self.interface}.")
                return
        if self._server:
            self._server.extra_latency_ms = self.delay_ms
        self.observe(f"Latência simulada de {self.delay_ms}ms aplicada na camada de aplicação.")


class PacketLossExperiment(_BaseNetworkExperiment):
    name = "Injeção de perda de pacotes"
    hypothesis = (
        "O serviço/protocolo deve tolerar a perda de pacotes especificada através de "
        "retransmissão ou timeout controlado, sem falhas em cascata."
    )

    def inject(self) -> None:
        if self.real_mode:
            applied = self._apply_tc([f"loss", f"{self.loss_pct}%"])
            if applied:
                self.observe(f"tc netem loss={self.loss_pct}% aplicado em {self.interface}.")
                return
        if self._server:
            self._server.loss_pct = self.loss_pct
        self.observe(f"Perda de pacotes simulada de {self.loss_pct}% aplicada na camada de aplicação.")


class NetworkPartitionExperiment(_BaseNetworkExperiment):
    name = "Partição de rede (perda total)"
    hypothesis = (
        "Ao perder completamente a conectividade com uma dependência, o serviço deve "
        "degradar de forma controlada (circuit breaker / timeout) em vez de travar ou "
        "consumir recursos indefinidamente."
    )

    def inject(self) -> None:
        if self.real_mode:
            applied = self._apply_tc(["loss", "100%"])
            if applied:
                self.observe(f"tc netem loss=100% aplicado em {self.interface} (partição total).")
                return
        if self._server:
            self._server.loss_pct = 100
        self.observe("Partição de rede total simulada na camada de aplicação (100% de perda).")
