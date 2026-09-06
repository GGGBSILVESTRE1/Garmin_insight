#!/usr/bin/env python
"""
Confere as unidades das atividades vindas da API do Garmin contra o Strava.

Por que existe: o export GDPR do Garmin guarda distância em centímetros,
durações em milissegundos, velocidade a 1/10 do m/s e calorias a 10x o valor
real. A API do Connect **não** faz nada disso — devolve valores reais. Essa
diferença é a fonte de erro mais provável de toda a ingestão, e um fator de 100
na distância passaria despercebido em qualquer gráfico agregado.

Como confere: as mesmas atividades existem nas duas fontes (o relógio sobe para
o Strava sozinho). O script casa por horário de início, calcula a razão entre os
valores e diz se ela é 1 — ou se é 100, 1000, 10, o que denunciaria escala.

É a mesma técnica que validou as unidades do export GDPR, registrada no
CLAUDE.md ("confirmado contra o Strava em 20/20 atividades sobrepostas").

Uso:
    python scripts/verificar_unidades_garmin.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import timedelta

from config import GARMIN_API_DIR, STRAVA_DIR
from ingestion.garmin_api import GarminApiIngestion
from ingestion.strava import StravaIngestion

TOLERANCIA_HORARIO = timedelta(minutes=10)

# Razão garmin/strava esperada em cada campo, e o que um desvio significaria
CAMPOS = [
    ("distance_meters", "distância"),
    ("duration_seconds", "duração"),
    ("elevation_gain_meters", "ganho de elevação"),
]

# Escalas conhecidas do export GDPR — se alguma aparecer, o parser da API está
# aplicando (ou deixando de aplicar) a conversão errada
SUSPEITAS = {100: "centímetros (escala do export GDPR)",
             1000: "milissegundos (escala do export GDPR)",
             10: "décimos (escala do export GDPR)",
             0.01: "conversão aplicada duas vezes",
             0.001: "conversão aplicada duas vezes"}


def _classificar(razao: float) -> str:
    if 0.97 <= razao <= 1.03:
        return "OK — mesma unidade"
    for fator, explicacao in SUSPEITAS.items():
        if abs(razao - fator) / fator < 0.05:
            return f"ERRO — fator {fator:g}x: {explicacao}"
    return f"SUSPEITO — razão {razao:.3f}, sem escala conhecida"


def main() -> int:
    if not GARMIN_API_DIR.exists():
        print("Nenhum cache da API em dados/garmin_api/.")
        print("Rode antes: python scripts/garmin_sync.py")
        return 1

    garmin = GarminApiIngestion(GARMIN_API_DIR).load_activities()
    strava = StravaIngestion(STRAVA_DIR).load_export_json()

    if not garmin:
        print("Cache da API sem atividades. Rode: python scripts/garmin_sync.py")
        return 1

    print(f"{len(garmin)} atividades da API x {len(strava)} do Strava")

    pares = []
    for g in garmin:
        gemea = next(
            (s for s in strava if abs(g.start_time - s.start_time) <= TOLERANCIA_HORARIO),
            None,
        )
        if gemea is not None:
            pares.append((g, gemea))

    print(f"{len(pares)} atividades presentes nas duas fontes\n")

    if not pares:
        print("Nenhuma atividade em comum — não há como comparar.")
        print("O período da API não encosta no que o Strava já tem. Ou rode")
        print("`python scripts/strava_export.py` para trazer o Strava até hoje, ou")
        print("`python scripts/garmin_sync.py --desde AAAA-MM-DD` num período coberto pelos dois.")
        return 1

    # Amostra pequena não invalida a checagem: um fator de 100 aparece com um
    # par só. O que ela não garante é o caso sutil — um campo que diverge em
    # 5% por arredondamento de uma fonte, não por unidade.
    amostra_pequena = len(pares) < 5
    if amostra_pequena:
        print(f"AVISO: só {len(pares)} par(es). Erro de escala grosseiro (100x, 1000x)")
        print("aparece mesmo assim, mas divergência sutil pode passar.")
        print("Para uma checagem forte, amplie a sobreposição entre as duas fontes.\n")

    problemas = 0
    for campo, rotulo in CAMPOS:
        razoes = []
        for g, s in pares:
            vg, vs = getattr(g, campo), getattr(s, campo)
            if vg and vs and vs != 0:
                razoes.append(vg / vs)

        if not razoes:
            print(f"{rotulo:20} sem dado nas duas fontes para comparar")
            continue

        mediana = statistics.median(razoes)
        veredito = _classificar(mediana)
        if not veredito.startswith("OK"):
            problemas += 1

        print(f"{rotulo:20} razão garmin/strava = {mediana:.4f}  (n={len(razoes)})")
        print(f"{'':20} {veredito}")

        # Um exemplo concreto ajuda a ler o número
        g, s = pares[0]
        vg, vs = getattr(g, campo), getattr(s, campo)
        if vg and vs:
            print(f"{'':20} ex.: garmin={vg:.1f}  strava={vs:.1f}  ({g.start_time.date()})")
        print()

    # Calorias não existem no Strava — a checagem possível é de plausibilidade
    print("calorias (só o Garmin tem — checagem de plausibilidade):")
    por_minuto = [
        a.calories / a.duration_minutes
        for a in garmin
        if a.calories and a.duration_minutes > 5
    ]
    if por_minuto:
        mediana = statistics.median(por_minuto)
        estado = "OK" if 2 <= mediana <= 15 else "ERRO — fora da faixa fisiológica"
        print(f"{'':20} mediana {mediana:.1f} kcal/min  ({estado})")
        if not 2 <= mediana <= 15:
            problemas += 1
            print(f"{'':20} 2-15 kcal/min é o plausível; ~40 indicaria o fator 10x do export")
    else:
        print(f"{'':20} nenhuma atividade com calorias")

    print()
    if amostra_pequena and not problemas:
        print(f"Nada errado nos {len(pares)} par(es) comparados — nenhuma escala suspeita.")
        print("Amostra pequena: repita depois de ampliar a sobreposição.")
        return 0

    if problemas:
        print(f"{problemas} campo(s) com unidade suspeita — ajuste _parse_atividade()")
        print("em src/ingestion/garmin_api.py antes de confiar nos CSVs.")
        return 1

    print("Todas as unidades conferem com o Strava.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
