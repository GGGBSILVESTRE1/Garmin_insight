#!/usr/bin/env python
"""
Sincronização semanal com o Garmin Connect: wellness e atividades.

Duas fontes possíveis, escolhidas por --fonte (padrão: auto):

- personal: a Personal API oficial (chave em GARMIN_PERSONAL_API_KEY). Sem
  MFA, token renovado sozinho — é o que deixa o agendador rodar sem você.
  O que ela não entrega (VO2max) cai no garminconnect, se houver sessão.
- garminconnect: a biblioteca não-oficial, como antes.

auto = personal quando a chave existe no .env, garminconnect quando não.

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
    python scripts/garmin_sync.py --fonte garminconnect  # forçar o não-oficial
    python scripts/garmin_sync.py --sem-fit          # não baixar os arquivos FIT

Fonte personal: requer GARMIN_PERSONAL_API_KEY no .env. A chave expira (30 a
365 dias, você escolhe ao gerar); quando expirar, o script falha dizendo isso.

Fonte garminconnect: requer GARMIN_EMAIL e GARMIN_PASSWORD no .env. A primeira
execução tem que ser manual (o login pode pedir MFA); depois a sessão fica salva.

Agendamento no Windows (semanal):
    Agendador de Tarefas → Criar Tarefa Básica → Semanalmente
    Programa:   <caminho>\\.venv\\Scripts\\python.exe
    Argumentos: scripts\\garmin_sync.py
    Iniciar em: <caminho do athlete-coach>
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from dotenv import load_dotenv

from config import ENV_FILE, GARMIN_API_DIR, GARMIN_TOKENS
from ingestion.garmin_api import GarminApiIngestion
from ingestion.garmin_personal import ClienteHibrido, ClientePersonal

load_dotenv(ENV_FILE)


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"data inválida: {texto} (use AAAA-MM-DD)") from None


def _montar_cliente(escolha: str):
    """
    Cliente da sincronização. None = deixa o GarminApiIngestion criar o
    garminconnect sozinho (comportamento antigo).
    """
    tem_chave = bool(os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip())
    if escolha == "garminconnect" or (escolha == "auto" and not tem_chave):
        return None

    personal = ClientePersonal.do_ambiente()
    # Fallback para o que a Personal não tem (VO2max). Só loga se for pedido,
    # e se falhar (sessão expirada, MFA) derruba só aquele endpoint.
    tem_login = (
        bool(os.environ.get("GARMIN_EMAIL", "").strip())
        or GARMIN_TOKENS.exists()
    )
    fabrica = GarminApiIngestion(GARMIN_API_DIR)._obter_cliente if tem_login else None
    print(f"Fonte: Personal API{' + garminconnect para VO2max' if fabrica else ''}")
    return ClienteHibrido(personal, fabrica_fallback=fabrica)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--desde", type=_data, help="primeiro dia a buscar (AAAA-MM-DD)")
    parser.add_argument("--ate", type=_data, help="último dia a buscar (padrão: ontem)")
    parser.add_argument("--silencioso", action="store_true", help="não listar dia a dia")
    parser.add_argument("--so-wellness", action="store_true", help="pular as atividades")
    parser.add_argument("--so-atividades", action="store_true", help="pular o wellness")
    parser.add_argument("--fonte", choices=["auto", "personal", "garminconnect"],
                        default="auto", help="de onde buscar (padrão: auto)")
    parser.add_argument("--sem-fit", action="store_true", help="não baixar os arquivos FIT")
    args = parser.parse_args()

    try:
        fonte = GarminApiIngestion(GARMIN_API_DIR, cliente=_montar_cliente(args.fonte))
    except RuntimeError as erro:
        print(f"\nERRO: {erro}", file=sys.stderr)
        return 1

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
        if relatorio["indisponiveis"]:
            print(f"  sem fonte para: {', '.join(sorted(set(relatorio['indisponiveis'])))}")
        falhas += relatorio["falhas"]

    if not args.so_wellness:
        print("\n[atividades]")
        try:
            relatorio = fonte.sync_activities(
                ate=args.ate, desde=args.desde, verbose=not args.silencioso,
                baixar_fit=not args.sem_fit,
            )
        except RuntimeError as erro:
            print(f"\nERRO: {erro}", file=sys.stderr)
            return 1
        print(f"  {relatorio['atividades']} atividades na janela: "
              f"{relatorio['novas']} novas, {relatorio['alteradas']} alteradas, "
              f"{len(relatorio['removidas'])} removidas no Connect")
        print(f"  {relatorio['detalhes']} blocos de detalhe e {relatorio['fits']} FIT baixados")
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
