# Chaos Engineering Simulator

Simulador local de chaos engineering em Python: injeta falhas controladas de
**processo**, **rede** e **recursos (CPU/memória/disco)**, mede o impacto e
gera **relatórios Markdown documentados** ("game day reports") para cada
experimento.

Projetado para rodar com segurança em qualquer máquina — sem depender de um
cluster Kubernetes real. Onde possível (Linux com `tc`/`iptables` e
privilégios), os experimentos de rede usam ferramentas reais do kernel; caso
contrário, caem automaticamente em um modo simulado que reproduz o mesmo
impacto na camada de aplicação.

## Instalação

```bash
cd chaos-simulator
python -m venv .venv && source .venv/bin/activate   # opcional
pip install -r requirements.txt
```

## Uso rápido

Listar experimentos disponíveis:

```bash
python -m chaos.cli list
```

Rodar um experimento único:

```bash
# Mata um processo de demonstração e observa se ele volta
python -m chaos.cli run process-kill --duration 5

# Crash-loop: mata e relança o processo repetidamente por 10s
python -m chaos.cli run process-crash-loop --duration 10 --crash-interval 2

# Estresse de CPU com 2 workers por 8 segundos
python -m chaos.cli run cpu-stress --duration 8 --workers 2

# Aloca ~256MB de memória por 5 segundos
python -m chaos.cli run memory-stress --duration 5 --target-mb 256

# Preenche 512MB de disco por 5 segundos
python -m chaos.cli run disk-fill --duration 5 --target-mb 512

# Injeta 200ms de latência de rede por 10 segundos
python -m chaos.cli run network-latency --duration 10 --delay-ms 200

# Injeta 15% de perda de pacotes por 10 segundos
python -m chaos.cli run packet-loss --duration 10 --loss-pct 15

# Simula uma partição de rede total (100% de perda) por 5 segundos
python -m chaos.cli run network-partition --duration 5

# Modo dry-run (não injeta falha de verdade, só valida o fluxo)
python -m chaos.cli run process-kill --dry-run
```

Rodar uma suíte completa ("game day") a partir de um YAML:

```bash
python -m chaos.cli gameday config/gameday_example.yaml
```

Cada execução gera um relatório em `reports/`, no formato:

```
reports/2026-07-13T10-00-00_process-a1b2c3d4.md
reports/2026-07-13T10-00-00_process-a1b2c3d4.json
```

O `.md` é o relatório legível (hipótese, parâmetros, linha do tempo,
steady-state antes/depois, métricas e conclusão). O `.json` contém os
mesmos dados de forma estruturada, útil para agregação ou dashboards.

## Arquitetura

```
chaos/
  core.py                 # Experiment (classe base), MetricsCollector, ReportGenerator
  experiments/
    process.py             # ProcessKillExperiment, ProcessCrashLoopExperiment
    network.py              # NetworkLatencyExperiment, PacketLossExperiment, NetworkPartitionExperiment
    resource.py              # CPUStressExperiment, MemoryStressExperiment, DiskFillExperiment
    _victim_service.py       # processo de demonstração usado pelos experimentos de processo
  cli.py                     # interface de linha de comando
config/
  gameday_example.yaml        # exemplo de suíte de experimentos
reports/                       # relatórios gerados (.md + .json)
```

Todo experimento segue o mesmo ciclo de vida (definido em `Experiment.run()`):

1. `check_prerequisites()` — validações de segurança antes de rodar.
2. `steady_state()` — mede o estado "normal" do sistema (baseline).
3. `inject()` — aplica a falha.
4. `probe()` — observa o comportamento repetidamente durante a duração.
5. `rollback()` — reverte a falha (sempre executado, mesmo em caso de erro).
6. `steady_state()` novamente — mede o estado pós-experimento.
7. `validate()` — compara antes/depois e decide status (`success` /
   `failed` / `aborted`) e conclusão.

## Extensão para Kubernetes / VMs reais

O framework foi desenhado para ser estendido com um "provider" real:

- **Kubernetes**: substitua `_resolve_pid()`/`inject()` em
  `ProcessKillExperiment` por chamadas ao `kubernetes` client Python
  (`delete_namespaced_pod`), e os experimentos de rede por manifests do
  [Chaos Mesh](https://chaos-mesh.org/) ou [Litmus](https://litmuschaos.io/)
  aplicados via `kubectl apply`.
- **VMs**: rode os experimentos de rede em modo "real" (`tc`/`iptables`)
  diretamente na VM alvo via SSH, ou distribua o simulador como um agente
  local em cada VM.

A classe base `Experiment` já foi projetada para não mudar nesses cenários —
apenas os métodos `inject()`/`rollback()`/`steady_state()` de cada
experimento precisam de uma nova implementação ("provider").

## Limites de segurança

- Estresse de memória nunca ultrapassa 85% do total de RAM da máquina
  (`MAX_MEMORY_PERCENT` em `resource.py`).
- Preenchimento de disco é limitado a 2GB e a 80% do espaço livre disponível
  (`MAX_DISK_FILL_MB` em `resource.py`).
- `process-kill` sem `--pid`/`--process-name` sempre sobe seu próprio
  processo de demonstração — nunca mata processos reais do sistema por
  engano.
- Todo `rollback()` roda em `finally`, então a falha é sempre revertida
  mesmo que o experimento seja interrompido por um erro.
