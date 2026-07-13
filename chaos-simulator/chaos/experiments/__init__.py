from .process import ProcessKillExperiment, ProcessCrashLoopExperiment
from .network import NetworkLatencyExperiment, PacketLossExperiment, NetworkPartitionExperiment
from .resource import CPUStressExperiment, MemoryStressExperiment, DiskFillExperiment

REGISTRY = {
    "process-kill": ProcessKillExperiment,
    "process-crash-loop": ProcessCrashLoopExperiment,
    "network-latency": NetworkLatencyExperiment,
    "packet-loss": PacketLossExperiment,
    "network-partition": NetworkPartitionExperiment,
    "cpu-stress": CPUStressExperiment,
    "memory-stress": MemoryStressExperiment,
    "disk-fill": DiskFillExperiment,
}

__all__ = [
    "ProcessKillExperiment",
    "ProcessCrashLoopExperiment",
    "NetworkLatencyExperiment",
    "PacketLossExperiment",
    "NetworkPartitionExperiment",
    "CPUStressExperiment",
    "MemoryStressExperiment",
    "DiskFillExperiment",
    "REGISTRY",
]
