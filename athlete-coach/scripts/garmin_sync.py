#!/usr/bin/env python
"""
Sincronização semanal pela API do Garmin Connect: wellness e atividades.

Baixa o que falta desde a última execução e grava o JSON cru em
dados/garmin_api/. Não reprocessa nada: depois do sync, rode
`python scripts/export_context.py` para reconstruir os CSVs.

As atividades vêm daqui, e não do Strava, porque só o Garmin traz FC, calorias,
zonas, training effect e RPE — no export do Strava esses campos estão vazios em
praticamente todas as atividades.

Uso:
    python scripts/garmin_sync.py                    # o que falta desde a última vez
    python scripts/garmin_sync.py --desde 2026-08-01 # preencher um buraco maior
    python scripts/garmin_sync.py --ate 2026-08-31   # parar antes de ontem
    python scripts/garmin_sync.py --so-atividades    # pular o wellness

Requer GARMIN_EMAIL e GARMIN_PASSWORD no .env (veja .env.example).

Primeira execução: rode **manualmente**, não pelo agendador. O login pode pedir
MFA e precisa de terminal. Depois disso a sessão fica salva e as execuções
agendadas passam a funcionar sozinhas — até o token expirar, quando o script
falha pedindo outra execução manual.

Agendamento no Windows (semanal):
    Agendador de Tarefas → Criar Tarefa Básica → Semanalmente
    Programa:   <caminho>\\.venv\\Scripts\\python.exe
    Argumentos: scripts\\garmin_sync.py
    Iniciar em: <caminho do athlete-coach>
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from dotenv import load_dotenv

from config import ENV_FILE, GARMIN_API_DIR
from ingestion.garmin_api import GarminApiIngestion

load_dotenv(ENV_FILE)


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"data inválida: {texto} (use AAAA-MM-DD)") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--desde", type=_data, help="primeiro dia a buscar (AAAA-MM-DD)")
    parser.add_argument("--ate", type=_data, help="último dia a buscar (padrão: ontem)")
    parser.add_argument("--silencioso", action="store_true", help="não listar dia a dia")
    parser.add_argument("--so-wellness", action="store_true", help="pular as atividades")
    parser.add_argument("--so-atividades", action="store_true", help="pular o wellness")
    args = parser.parse_args()

    fonte = GarminApiIngestion(GARMIN_API_DIR)

    falhas = []

    if not args.so_atividades:
        print(f"\n[wellness] {GARMIN_API_DIR}")
        try:
            relatorio = fonte.sync(ate=args.ate, desde=args.desde, verbose=not args.silencioso)
        except RuntimeError as erro:
            print(f"\nERRO: {erro}", file=sys.stderr)
            return 1
        print(f"  {relatorio['baixados']} dia-endpoint baixados, "
              f"{relatorio['pulados']} já em cache")
        falhas += relatorio["falhas"]

    if not args.so_wellness:
        print("\n[atividades]")
        try:
            relatorio = fonte.sync_activities(
                ate=args.ate, desde=args.desde, verbose=not args.silencioso
            )
        except RuntimeError as erro:
            print(f"\nERRO: {erro}", file=sys.stderr)
            return 1
        print(f"  {relatorio['atividades']} atividades, "
              f"{relatorio['detalhes']} blocos de detalhe novos")
        falhas += relatorio["falhas"]

    if falhas:
        print(f"\n{len(falhas)} falha(s):")
        for origem, alvo, mensagem in falhas[:15]:
            print(f"  {origem:14} {alvo}  {mensagem}")
        if len(falhas) > 15:
            print(f"  ... e mais {len(falhas) - 15}")
        print("\nFalhas isoladas são normais (dia sem registro, atividade sem potência).")
        print("Falha em tudo indica sessão expirada ou mudança na API.")

    # Confere o que o cache já rende, sem tocar a rede
    wellness = fonte.load_all()
    if wellness:
        dias = sorted(wellness)
        print(f"\nCache local: {len(dias)} dias de wellness ({dias[0]} a {dias[-1]})")

    atividades = fonte.load_activities()
    if atividades:
        print(f"             {len(atividades)} atividades "
              f"({atividades[0].start_time.date()} a {atividades[-1].start_time.date()})")

    print("\nPróximo passo: python scripts/export_context.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
