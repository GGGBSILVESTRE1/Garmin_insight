"""
Carregamento das tabelas de `dados/processed` para os notebooks.

Regra do projeto: **nenhum notebook chama `pd.read_csv`**. Cada tabela tem um
`load_*()` aqui, que resolve o caminho pelo `config`, converte as datas e aplica
`format_week()` com a mesma âncora de semana. Assim a mesma tabela chega igual
em qualquer notebook — mesmas colunas, mesmos tipos, mesmo significado de
`semana` — e uma correção de leitura é feita num lugar só.

Uso típico:

    from processing.datasets import load_activities, load_hr_zones

    activities = load_activities()
    hr_zones = load_hr_zones()

Convenção de nome no notebook: a variável tem o nome da tabela (`activities`,
`wellness`, `hr_zones`), e recortes derivados levam o nome do filtro (`runs`,
`base_runs`). Sem prefixo `df_` — todas são DataFrames.
"""

from functools import lru_cache

import pandas as pd

from config import PROCESSED_DIR
from processing.features import add_lags, format_week

# Tabela -> arquivo em dados/processed. Serve de índice para os loaders e para o
# notebook de inventário, que percorre tudo sem repetir nomes de arquivo.
ARQUIVOS = {
    "activities": "activities.csv",
    "hr_zones": "activity_hr_zones.csv",
    "power_zones": "activity_power_zones.csv",
    "strength_sets": "strength_sets.csv",
    "training_load": "training_load_daily.csv",
    "weekly_summary": "weekly_summary.csv",
    "wellness": "wellness_daily.csv",
}

# Grão de cada tabela — o que uma linha representa. É o que decide se um
# groupby faz sentido e se um merge é 1:1 ou N:1.
GRAO = {
    "activities": "uma linha por atividade",
    "hr_zones": "uma linha por (atividade, zona de FC)",
    "power_zones": "uma linha por (atividade, zona de potência)",
    "strength_sets": "uma linha por (atividade, exercício)",
    "training_load": "uma linha por dia do calendário",
    "weekly_summary": "uma linha por semana",
    "wellness": "uma linha por dia com registro do relógio",
}


@lru_cache(maxsize=1)
def week_origin() -> pd.Timestamp:
    """
    Segunda-feira da atividade mais antiga — âncora comum da numeração de semanas.

    Todas as tabelas usam esta mesma data, então `semana = 30` significa a mesma
    semana do calendário em `wellness`, `activities` e `training_load`. Sem isso
    cada tabela contaria a partir do próprio início e o cruzamento por semana
    juntaria períodos diferentes sem reclamar.
    """
    datas = pd.read_csv(
        PROCESSED_DIR / ARQUIVOS["activities"], usecols=["date"], parse_dates=["date"]
    )["date"]
    primeira = datas.min()
    return primeira - pd.Timedelta(days=primeira.weekday())


def _ler(tabela: str, date_col: str = "date") -> pd.DataFrame:
    """Lê o CSV da tabela, converte a data e adiciona as colunas de semana."""
    df = pd.read_csv(PROCESSED_DIR / ARQUIVOS[tabela], parse_dates=[date_col])
    return format_week(df, date_col=date_col, origin=week_origin())


def load_activities() -> pd.DataFrame:
    """
    Uma linha por treino, Garmin e Strava já unificados (83 colunas).

    Tabela larga de propósito: colunas de corrida, natação e musculação convivem
    no mesmo arquivo, cada uma preenchida só onde faz sentido. Filtre por
    `sport` antes de olhar para nulos, senão a leitura de completude engana.
    """
    return _ler("activities")


def load_hr_zones() -> pd.DataFrame:
    """
    Formato longo: uma linha por (atividade, zona de FC), com segundos e minutos.

    `zone_0` é o tempo abaixo de Z1 — pausa, semáforo, descanso entre séries.
    Quase toda análise de intensidade quer excluir essa faixa.
    """
    return _ler("hr_zones")


def load_power_zones() -> pd.DataFrame:
    """Mesmo formato longo das zonas de FC, para potência (só onde há medidor)."""
    return _ler("power_zones")


def load_strength_sets() -> pd.DataFrame:
    """Séries de musculação por exercício: séries, repetições, volume e carga máxima."""
    return _ler("strength_sets")


def load_training_load() -> pd.DataFrame:
    """
    Série diária de TSS, CTL (fitness), ATL (fadiga) e TSB (forma).

    Calendário completo: dias sem treino aparecem com TSS 0 e CTL/ATL decaindo,
    que é justamente o que faz as médias exponenciais serem comparáveis.
    """
    return _ler("training_load")


def load_weekly_summary() -> pd.DataFrame:
    """Agregado semanal pronto (volume, duração, pace médio). Data em `week_start`."""
    return _ler("weekly_summary", date_col="week_start")


def load_wellness() -> pd.DataFrame:
    """
    Uma linha por dia: sono e estágios, HRV, FC de repouso, estresse, passos, VO2max.

    Só existe onde o relógio registrou — a janela é bem mais curta que a das
    atividades, e algumas colunas (`spo2`, `vo2max`) são esparsas ou vazias.
    """
    return _ler("wellness")


LOADERS = {
    "activities": load_activities,
    "hr_zones": load_hr_zones,
    "power_zones": load_power_zones,
    "strength_sets": load_strength_sets,
    "training_load": load_training_load,
    "weekly_summary": load_weekly_summary,
    "wellness": load_wellness,
}


def load_all() -> dict[str, pd.DataFrame]:
    """Todas as tabelas de uma vez, para inventário e conferência de cobertura."""
    return {nome: load() for nome, load in LOADERS.items()}


def load_daily(lag_cols=None, verbose=True) -> pd.DataFrame:
    """
    Wellness + carga de treino no mesmo dia, com as variáveis do dia anterior.

    Três coisas acontecem aqui, e todas mudam o resultado de uma correlação:

    1. **Merge `inner` validado 1:1** — só dias com wellness *e* carga. O
       `validate` garante que nenhuma data se repete; se repetisse, o merge
       duplicaria linhas em silêncio e inflaria todo agregado seguinte.
    2. **Calendário completo** — o intervalo é reindexado dia a dia. Sem isso,
       um dia sem registro faria `shift(1)` puxar o valor de dois ou três dias
       antes, e o "lag1" deixaria de ser ontem.
    3. **Lags** — `<coluna>_lag1` com o valor de ontem, para testar se a carga
       de ontem explica a recuperação de hoje.
    """
    if lag_cols is None:
        lag_cols = ["hrv", "sleep_hours", "sleep_score", "atl", "ctl", "tsb", "tss"]

    wellness = load_wellness()
    training_load = load_training_load()

    daily = wellness.merge(
        training_load,
        on="date",
        how="inner",
        validate="one_to_one",
        suffixes=("", "_load"),
    )
    daily = daily.drop(columns=[c for c in daily.columns if c.endswith("_load")])
    dias_com_dado = len(daily)

    # Calendário completo entre o primeiro e o último dia da janela comum
    calendario = pd.date_range(daily["date"].min(), daily["date"].max(), freq="D")
    daily = (
        daily.set_index("date")
        .reindex(calendario)
        .rename_axis("date")
        .reset_index()
    )
    daily = format_week(daily, origin=week_origin())

    daily = add_lags(daily, lag_cols, n=1)

    if verbose:
        buracos = len(daily) - dias_com_dado
        print(
            f"{dias_com_dado} dias com wellness + carga "
            f"({daily['date'].min().date()} a {daily['date'].max().date()})"
            + (f", {buracos} dia(s) sem registro preenchidos com NaN" if buracos else "")
        )

    return daily


def join_activities(df, activities=None) -> pd.DataFrame:
    """
    Traz as colunas de `activities` para uma tabela que só tem `activity_id`.

    As tabelas de zonas e de séries guardam pouco sobre o treino em si — para
    cruzar zona com distância, pace ou TSS é preciso este join.

    `validate="many_to_one"` é a parte que importa: garante que cada
    `activity_id` aparece uma única vez em `activities`. Com duplicata, o merge
    multiplicaria linhas sem avisar e todo agregado depois sairia inflado.

    As colunas de semana do lado direito são descartadas: já vêm iguais do lado
    esquerdo, porque os dois loaders usam a mesma âncora.
    """
    if activities is None:
        activities = load_activities()

    juntado = df.merge(
        activities,
        on="activity_id",
        how="left",
        validate="many_to_one",
        suffixes=("", "_act"),
    )
    return juntado.drop(columns=[c for c in juntado.columns if c.endswith("_act")])
