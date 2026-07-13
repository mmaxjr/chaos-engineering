"""
Processo "vítima" de demonstração usado pelos experimentos de falha de processo.
Simplesmente fica vivo consumindo CPU/memória mínimas até ser encerrado.
Não faz parte da API pública do pacote.
"""
import time

if __name__ == "__main__":
    while True:
        time.sleep(0.2)
