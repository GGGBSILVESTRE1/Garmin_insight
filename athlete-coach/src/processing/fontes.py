"""
Junção das fontes de atividade e wellness num conjunto só, sem duplicatas.

Morava dentro de `scripts/export_context.py`. Veio para `src/` porque é regra
de dado, não de apresentação: notebook, app e teste precisam da mesma junção,
e script não se importa.

Duas deduplicações, de naturezas diferentes:

1. **Garmin × Garmin** (Personal API / garminconnect × export GDPR): exata, pelo
   `activityId`. O cache da API vence, por ser a fonte viva.
2. **Garmin × Strava**: heurística (início ±10 min, distância ±200 m), porque
   os ids não se conversam — o `external_id` do Strava (`garmin_ping_<n>`) não
   é o `activityId` do Garmin (conferido: 0 de 106 batem). O par é **fundido**,
   com o Strava como base e o Garmin preenchendo o que falta.

O casamento Garmin × Strava é 1:1 e exige categoria compatível. Antes ele
pegava o primeiro Strava dentro da tolerância: um mesmo Strava podia absorver
duas atividades Garmin, e uma musculação (distância 0) casava com qualquer
coisa curta que começasse perto.
"""
from __future__ import annotations

from config import GARMIN_API_DIR, GARMIN_DIR, STRAVA_DIR
from ingestion.classificacao import aplicar, classificar
from ingestion.garmin import GarminIngestion
from ingestion.garmin_api import GarminApiIngestion
from ingestion.garmin_wellness import GarminWellnessIngestion
from ingestion.strava import StravaIngestion

# Tolerâncias do casamento Garmin × Strava
TOLERANCIA_INICIO_S = 600
TOLERANCIA_DISTANCIA_M = 200

# Categorias que o Strava e o Garmin costumam nomear diferente para a mesma
# sessão ("Workout" × "indoor_cardio" × "other"): casam com qualquer outra
_CATEGORIAS_CORINGA = {"outro", "cardio"}


def _compativeis(cat_a: str, cat_b: str) -> bool:
    return cat_a == cat_b or cat_a in _CATEGORIAS_CORINGA or cat_b in _CATEGORIAS_CORINGA


def carregar_atividades_garmin(verbose: bool = True) -> list:
    """
    Une as atividades das fontes Garmin: o cache da API e o export GDPR.

    Dedup exata por `activityId`; a API sobrepõe o export. As unidades já
    chegam reais dos dois parsers (o export guarda cm e kcal×10, a API não),
    então nada de escala acontece aqui.
    """
    api = GarminApiIngestion(GARMIN_API_DIR).load_activities() if GARMIN_API_DIR.exists() else []
    export = GarminIngestion(GARMIN_DIR).load_garmin_connect_json()

    por_id = {a.activity_id: a for a in export}
    novas = 0
    for atividade in api:
        if atividade.activity_id not in por_id:
            novas += 1
        por_id[atividade.activity_id] = atividade  # API sobrepõe o export

    if api and verbose:
        print(f"  atividades Garmin: {len(api)} da API ({novas} fora do export) "
              f"+ {len(export)} do export = {len(por_id)} únicas")

    return sorted(por_id.values(), key=lambda a: a.start_time)


def carregar_wellness(verbose: bool = True) -> dict:
    """
    Funde o wellness da API e do export GDPR. A API vem primeiro de propósito:
    `_merge` só preenche campo vazio, então quem chega antes tem precedência.
    """
    api = GarminApiIngestion(GARMIN_API_DIR).load_all() if GARMIN_API_DIR.exists() else {}
    export = GarminWellnessIngestion(GARMIN_DIR).load_all()

    registros: dict = {}
    GarminWellnessIngestion._merge(registros, api)
    GarminWellnessIngestion._merge(registros, export)

    if api and verbose:
        print(f"  wellness: {len(api)} dias da API + {len(export)} do export "
              f"= {len(registros)} únicos")

    return dict(sorted(registros.items()))


def fundir_garmin_no_strava(garmin: list, strava: list,
                            tol_inicio_s: int = TOLERANCIA_INICIO_S,
                            tol_distancia_m: float = TOLERANCIA_DISTANCIA_M,
                            verbose: bool = True) -> list:
    """
    Funde cada atividade Garmin no seu par do Strava, mantendo uma linha só.

    O relógio sobe o treino para o Strava sozinho, então a mesma sessão aparece
    nas duas fontes. Contar as duas dobraria CTL/ATL/TSB; descartar o lado
    Garmin perderia o que o Strava nunca recebe (zonas, RPE, séries, calorias).

    Casamento 1:1: cada Strava aceita no máximo um Garmin, e entre os
    candidatos ganha o de início mais próximo. **Modifica os objetos do Strava
    in place** (source vira "strava+garmin") e devolve as atividades Garmin
    sem par, que entram como registro próprio.
    """
    categoria = {id(a): classificar(a).categoria for a in [*garmin, *strava]}
    ja_casados: set[int] = set()
    sem_par = []
    fundidas = 0

    for g in garmin:
        candidatos = [
            s for s in strava
            if id(s) not in ja_casados
            and abs((g.start_time - s.start_time).total_seconds()) <= tol_inicio_s
            and abs(g.distance_meters - s.distance_meters) <= tol_distancia_m
            and _compativeis(categoria[id(g)], categoria[id(s)])
        ]
        if not candidatos:
            sem_par.append(g)
            continue

        par = min(candidatos, key=lambda s: abs((g.start_time - s.start_time).total_seconds()))
        ja_casados.add(id(par))

        # Campos principais que o Strava deixa vazios — calorias só vêm do Garmin
        for campo in ("calories", "avg_heart_rate", "max_heart_rate",
                      "elevation_gain_meters", "avg_power_watts"):
            if getattr(par, campo) is None:
                setattr(par, campo, getattr(g, campo))

        # Extras só do Garmin; nunca sobrescreve o que o Strava já trouxe
        for chave, valor in g.extra.items():
            par.extra.setdefault(chave, valor)
        par.extra["garmin_activity_id"] = g.activity_id
        par.source = "strava+garmin"
        fundidas += 1

    if verbose:
        if fundidas:
            print(f"  {fundidas} atividades sincronizadas Garmin->Strava (dados fundidos)")
        if sem_par:
            print(f"  {len(sem_par)} atividades so no Garmin")
    return sem_par


def montar_atividades(verbose: bool = True) -> list:
    """
    O conjunto final de atividades: Garmin + Strava, sem duplicata e já
    classificado (categoria, ambiente, flags em `extra`).

    A classificação roda **depois** da fusão de propósito: a atividade fundida
    herda o `activity_type` do Garmin, que é o sinal mais forte para separar
    esteira de rua.
    """
    garmin = carregar_atividades_garmin(verbose=verbose)
    strava = StravaIngestion(STRAVA_DIR).load_export_json()
    so_garmin = fundir_garmin_no_strava(garmin, strava, verbose=verbose)
    todas = sorted(so_garmin + strava, key=lambda a: a.start_time)
    return aplicar(todas)
