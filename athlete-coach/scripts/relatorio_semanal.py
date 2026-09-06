#!/usr/bin/env python
"""
Imprime o relatório da semana no terminal.

Lê só os CSVs de dados/processed/ — não toca a rede. Para dado novo, rode antes
`python scripts/garmin_sync.py` e `python scripts/export_context.py`.

Uso:
    python scripts/relatorio_semanal.py              # última semana completa
    python scripts/relatorio_semanal.py --parcial    # a semana em curso
    python scripts/relatorio_semanal.py --semana 113 # uma semana específica
    python scripts/relatorio_semanal.py --base 8     # linha de base mais longa
"""
from __future__ import annotations

import argparse
import sys

from processing.weekly import BASELINE_SEMANAS, formatar_relatorio, relatorio_semana


def main() -> int:
    parser = argparse.ArgumentParser(description="Relatório semanal de treino")
    parser.add_argument("--semana", type=int, help="número da semana (padrão: última completa)")
    parser.add_argument("--parcial", action="store_true", help="usar a semana em curso")
    parser.add_argument(
        "--base", type=int, default=BASELINE_SEMANAS,
        help=f"semanas anteriores na linha de base (padrão: {BASELINE_SEMANAS})",
    )
    args = parser.parse_args()

    try:
        relatorio = relatorio_semana(
            semana=args.semana, baseline=args.base, parcial=args.parcial
        )
    except ValueError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1

    print(formatar_relatorio(relatorio))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
