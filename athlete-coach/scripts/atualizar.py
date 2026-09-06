#!/usr/bin/env python
"""
Ponto de entrada único da atualização semanal — é isto que o agendador chama.

Encadeia o que hoje são três comandos manuais:

    1. garmin_sync.py     baixa atividades e wellness novos da API do Connect
    2. export_context.py  reprocessa e reescreve dados/processed/*.csv
    3. registra o resultado em dados/.ultima_execucao.json

O passo 3 é o que torna a automação confiável. Tarefa agendada falha em
silêncio: o token expira, nada roda, e a interface segue mostrando os números da
semana passada com a mesma cara de novo. Com o registro, o app diz "estes dados
têm 9 dias" em vez de deixar você decidir treino em cima de dado velho.

Cada etapa roda em subprocesso próprio. Uma falhar não impede o registro nem
esconde o que a outra fez — e a saída de cada uma vai para o log.

Uso:
    python scripts/atualizar.py                 # sincroniza e reprocessa
    python scripts/atualizar.py --sem-rede      # só reprocessa o que já está em disco
    python scripts/atualizar.py --relatorio     # imprime o relatório ao final

Agendamento no Windows (semanal, segunda de manhã):
    Agendador de Tarefas -> Criar Tarefa Basica -> Semanalmente
    Programa:   <raiz>\\.venv\\Scripts\\python.exe
    Argumentos: scripts\\atualizar.py
    Iniciar em: <raiz>\\athlete-coach

A PRIMEIRA execução tem que ser manual, no terminal: o login do Garmin pode
pedir MFA e precisa de alguém para digitar. Depois disso a sessão fica salva.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import ROOT
from processing.estado import registrar

SCRIPTS = Path(__file__).resolve().parent


def _rodar(nome: str, script: str, args: list[str] | None = None) -> dict:
    """
    Roda um script do projeto num subprocesso e devolve o resultado da etapa.

    Usa `sys.executable` para garantir o mesmo interpretador — no agendador não
    há venv ativado, e chamar "python" pegaria o do sistema, sem as dependências.
    """
    comando = [sys.executable, str(SCRIPTS / script), *(args or [])]
    print(f"\n{'=' * 62}\n{nome}\n{'=' * 62}")

    processo = subprocess.run(comando, cwd=ROOT, text=True, capture_output=True)

    if processo.stdout:
        print(processo.stdout.rstrip())
    if processo.stderr:
        print(processo.stderr.rstrip(), file=sys.stderr)

    ok = processo.returncode == 0
    detalhe = "ok" if ok else (processo.stderr or processo.stdout or "").strip()[-300:]

    return {"nome": nome, "ok": ok, "detalhe": detalhe}


def main() -> int:
    parser = argparse.ArgumentParser(description="Atualização semanal do athlete-coach")
    parser.add_argument("--sem-rede", action="store_true",
                        help="pula a sincronização e só reprocessa os CSVs")
    parser.add_argument("--relatorio", action="store_true",
                        help="imprime o relatório da semana ao final")
    args = parser.parse_args()

    inicio = datetime.now()
    print(f"Atualização iniciada em {inicio:%d/%m/%Y %H:%M}")

    etapas = []

    if not args.sem_rede:
        etapas.append(_rodar("Sincronizar Garmin", "garmin_sync.py", ["--silencioso"]))

    # Reprocessa mesmo se o sync falhou: o que já está em cache continua valendo,
    # e um CSV reconstruído a partir de dado antigo é melhor que nenhum CSV.
    etapas.append(_rodar("Reprocessar CSVs", "export_context.py"))

    estado = registrar(etapas)

    print(f"\n{'=' * 62}")
    for etapa in etapas:
        print(f"  {'OK  ' if etapa['ok'] else 'FALHA'} {etapa['nome']}")

    duracao = (datetime.now() - inicio).total_seconds()
    print(f"\nConcluída em {duracao:.0f}s — estado gravado.")

    if not estado["ok"]:
        print("\nAlguma etapa falhou. O app vai marcar os dados como suspeitos.")
        print("Se a falha for de sessão do Garmin, rode no terminal:")
        print("  python scripts/garmin_sync.py")
        return 1

    if args.relatorio:
        print()
        etapas.append(_rodar("Relatório da semana", "relatorio_semanal.py"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
