"""
Relatório de uma semana de treino, comparada com as semanas anteriores.

É a camada que os notebooks não têm. Eles mostram a série inteira e servem para
explorar; um relatório semanal responde outra pergunta — **como foi esta semana
em relação às últimas** — e por isso todo número aqui vem em par: o valor da
semana e a linha de base das semanas que a precedem.

Devolve dado, não texto nem gráfico. `formatar_relatorio()` renderiza em texto,
e um app pode desenhar a mesma estrutura sem recalcular nada. A regra é a
mesma dos loaders: a lógica mora aqui, a apresentação mora fora.

Uso:
    from processing.weekly import relatorio_semana, formatar_relatorio

    rel = relatorio_semana()                 # última semana completa
    print(formatar_relatorio(rel))

    rel = relatorio_semana(semana=117)       # uma semana específica
    rel = relatorio_semana(parcial=True)     # a semana em curso, com aviso
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from processing.datasets import (
    load_activities,
    load_hr_zones,
    load_training_load,
    load_wellness,
)
from processing.features import FAIXAS_POLARIZADAS

# ---------------------------------------------------------------------------
# Limiares
# ---------------------------------------------------------------------------
# Todos são julgamento, não fato — vêm da literatura de treinamento e da
# fisiologia deste atleta, e cada um vale uma discussão. Ficam aqui, nomeados e
# num lugar só, para poderem ser discordados sem caçar número no meio do código.

# Modelo polarizado: a faixa fácil deveria dominar a semana.
META_BAIXA_INTENSIDADE = 80.0   # % do tempo de corrida em Z1+Z2
LIMITE_GRAY_ZONE = 30.0         # acima disso, treino "sempre médio"

# Salto de volume. A regra dos 10% é conservadora demais para quem está
# construindo base; 30% acima da média de 4 semanas é onde o risco aparece.
SALTO_VOLUME_PCT = 30.0

# HRV abaixo da própria linha de base. Não existe valor absoluto bom: o que
# informa é o desvio contra as semanas recentes do mesmo atleta.
QUEDA_HRV_PCT = 7.0

# Forma (TSB = CTL - ATL). Fora dessa faixa há um recado.
TSB_FADIGA = -20.0     # abaixo: fadiga acumulada
TSB_DESTREINO = 20.0   # acima por muito tempo: perdendo estímulo

SONO_MINIMO_HORAS = 7.0

# Quantas semanas anteriores formam a linha de base. Quatro é o suficiente para
# média estável e curto o bastante para não comparar com uma forma que já passou.
BASELINE_SEMANAS = 4


# ---------------------------------------------------------------------------
# Estrutura do relatório
# ---------------------------------------------------------------------------

@dataclass
class Comparacao:
    """
    Um número da semana ao lado da linha de base.

    Toda métrica do relatório tem esta forma, o que dá à apresentação uma coisa
    só para renderizar: valor, base, e a variação já calculada. `None` em
    qualquer lado significa "sem dado", nunca zero.
    """
    valor: float | None
    base: float | None
    unidade: str = ""

    @property
    def delta(self) -> float | None:
        if self.valor is None or self.base is None:
            return None
        return self.valor - self.base

    @property
    def delta_pct(self) -> float | None:
        if self.valor is None or not self.base:
            return None
        return (self.valor - self.base) / self.base * 100

    def __str__(self) -> str:
        if self.valor is None:
            return "sem dado"

        texto = f"{self.valor:.1f}{self.unidade}"
        if self.base is None:
            return texto

        # Métrica que já é percentual compara em pontos percentuais. Dizer que
        # 3,9% virou 9,2% é "+134%" está certo aritmeticamente e é ilegível:
        # "+5,3 pp" é o que um treinador entende.
        if self.unidade == "%":
            variacao = f"{self.delta:+.1f} pp"
        else:
            variacao = f"{self.delta_pct:+.0f}%"

        return f"{texto} ({variacao} vs base {self.base:.1f}{self.unidade})"


@dataclass
class Observacao:
    """Uma leitura da semana, com o limiar que a disparou explicitado."""
    nivel: str      # "alerta" | "atencao" | "ok"
    titulo: str
    detalhe: str


@dataclass
class RelatorioSemanal:
    semana: int
    inicio: date
    fim: date
    completa: bool
    semanas_base: list[int]

    # Volume
    sessoes: Comparacao
    km_corrida: Comparacao
    minutos_treino: Comparacao

    # Intensidade — % do tempo de corrida em cada faixa do modelo polarizado
    baixa_intensidade: Comparacao
    gray_zone: Comparacao
    alta_intensidade: Comparacao

    # Carga
    tss: Comparacao
    ctl_fim: float | None
    atl_fim: float | None
    tsb_fim: float | None
    ctl_delta: float | None

    # Recuperação
    sono_horas: Comparacao
    sono_score: Comparacao
    hrv: Comparacao
    fc_repouso: Comparacao
    dias_wellness: int

    observacoes: list[Observacao] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agregação
# ---------------------------------------------------------------------------

def agregados_semanais() -> pd.DataFrame:
    """
    Uma linha por semana com todas as métricas do relatório.

    Tudo é calculado de uma vez, para todas as semanas, e o relatório passa a
    ser seleção de linha: a semana pedida e as anteriores. Sai mais simples que
    recalcular a mesma agregação para cada semana da linha de base, e garante
    que semana e base são medidas exatamente do mesmo jeito.

    As quatro fontes entram **separadas de propósito**, não pela interseção: uma
    semana com treino mas sem wellness ainda rende o bloco de carga, com os
    campos de recuperação vazios. Cruzar antes reduziria toda a série à janela
    mais curta.
    """
    activities = load_activities()
    hr_zones = load_hr_zones()
    training_load = load_training_load()
    wellness = load_wellness()

    # --- volume -----------------------------------------------------------
    corridas = activities[activities["sport"] == "Run"]
    volume = pd.DataFrame({
        "inicio": activities.groupby("semana")["comeco_semana"].min(),
        "sessoes": activities.groupby("semana")["activity_id"].count(),
        "minutos_treino": activities.groupby("semana")["duration_minutes"].sum(),
        "km_corrida": corridas.groupby("semana")["distance_km"].sum(),
    })
    # Semana com treino mas sem corrida (só musculação, por exemplo) correu
    # zero — não é dado faltando. O índice de `volume` vem das atividades, então
    # todo NaN aqui é ausência de corrida, não ausência de registro.
    volume["km_corrida"] = volume["km_corrida"].fillna(0.0)

    # --- intensidade ------------------------------------------------------
    # Só corridas e sem zone_0: musculação tem dinâmica de FC diferente, e
    # zone_0 é pausa/semáforo, não treino.
    zonas_corrida = hr_zones[
        (hr_zones["sport"] == "Run") & (hr_zones["zone"] != "zone_0")
    ]
    por_zona = zonas_corrida.groupby(["semana", "zone"])["minutes"].sum().unstack(fill_value=0)

    intensidade = pd.DataFrame(index=por_zona.index)
    total_semana = por_zona.sum(axis=1)
    for faixa, zonas in FAIXAS_POLARIZADAS.items():
        presentes = [z for z in zonas if z in por_zona.columns]
        soma = por_zona[presentes].sum(axis=1) if presentes else 0
        intensidade[faixa] = (soma / total_semana * 100).where(total_semana > 0)

    intensidade.columns = ["pct_baixa", "pct_gray", "pct_alta"]

    # --- carga ------------------------------------------------------------
    # CTL/ATL/TSB são estado, não soma: o que vale é o valor no fim da semana.
    # O TSS, sim, é acumulado. `ctl_delta` mede o quanto a semana construiu.
    carga = pd.DataFrame({
        "tss": training_load.groupby("semana")["tss"].sum(),
        "ctl_fim": training_load.groupby("semana")["ctl"].last(),
        "atl_fim": training_load.groupby("semana")["atl"].last(),
        "tsb_fim": training_load.groupby("semana")["tsb"].last(),
        "ctl_inicio": training_load.groupby("semana")["ctl"].first(),
    })
    carga["ctl_delta"] = carga["ctl_fim"] - carga["ctl_inicio"]

    # --- recuperação ------------------------------------------------------
    recuperacao = wellness.groupby("semana").agg(
        sono_horas=("sleep_hours", "mean"),
        sono_score=("sleep_score", "mean"),
        hrv=("hrv", "mean"),
        fc_repouso=("resting_hr", "mean"),
        dias_wellness=("date", "count"),
    )

    tabela = volume.join([intensidade, carga, recuperacao], how="outer")
    tabela["dias_wellness"] = tabela["dias_wellness"].fillna(0).astype(int)

    # `inicio` vem das atividades; semanas que só têm carga ou wellness ficam
    # sem ela, então é reconstruída a partir do número da semana.
    if tabela["inicio"].isna().any():
        tabela["inicio"] = tabela["inicio"].fillna(
            pd.Series(
                {s: _inicio_da_semana(s) for s in tabela.index[tabela["inicio"].isna()]}
            )
        )

    return tabela.sort_index()


def _inicio_da_semana(semana: int) -> pd.Timestamp:
    """Segunda-feira da semana N, usando a mesma âncora global dos loaders."""
    from processing.datasets import week_origin

    return week_origin() + pd.Timedelta(weeks=semana - 1)


def semanas_com_treino() -> list[int]:
    """Semanas que têm ao menos uma atividade registrada."""
    activities = load_activities()
    return sorted(activities["semana"].unique().tolist())


def ultima_semana_completa(hoje: date | None = None) -> int:
    """
    Número da última semana seg–dom já encerrada.

    Semana em curso não entra por padrão: hoje é quarta, ela tem dois dias, e
    comparar dois dias contra médias de sete produziria um relatório que mente
    sem avisar.
    """
    hoje = hoje or date.today()
    segunda_desta = hoje - timedelta(days=hoje.weekday())

    from processing.datasets import week_origin

    origem = week_origin().date()
    return ((segunda_desta - origem).days // 7 + 1) - 1


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def relatorio_semana(
    semana: int | None = None,
    baseline: int = BASELINE_SEMANAS,
    parcial: bool = False,
    hoje: date | None = None,
) -> RelatorioSemanal:
    """
    Monta o relatório de uma semana.

    Sem `semana`, usa a última completa — ou a em curso, se `parcial=True`, e
    nesse caso `RelatorioSemanal.completa` vem False para a apresentação poder
    avisar.

    A linha de base são as `baseline` semanas imediatamente anteriores, entre as
    que têm treino registrado. Semana sem treino é pulada em vez de entrar como
    zero: uma semana de férias no meio da base derrubaria a média e faria a
    semana seguinte parecer um salto de carga.
    """
    hoje = hoje or date.today()
    tabela = agregados_semanais()

    if semana is None:
        semana = ultima_semana_completa(hoje) + (1 if parcial else 0)

    if semana not in tabela.index:
        disponiveis = semanas_com_treino()
        raise ValueError(
            f"Semana {semana} não tem dado. Disponíveis: "
            f"{disponiveis[0]}–{disponiveis[-1]}"
        )

    atual = tabela.loc[semana]

    com_treino = [s for s in semanas_com_treino() if s < semana]
    semanas_base = com_treino[-baseline:]
    base = tabela.loc[semanas_base] if semanas_base else tabela.iloc[0:0]

    def cmp(coluna: str, unidade: str = "") -> Comparacao:
        return Comparacao(
            valor=_ou_none(atual.get(coluna)),
            base=_ou_none(base[coluna].mean()) if len(base) else None,
            unidade=unidade,
        )

    inicio = pd.Timestamp(atual["inicio"]).date()
    fim = inicio + timedelta(days=6)

    rel = RelatorioSemanal(
        semana=int(semana),
        inicio=inicio,
        fim=fim,
        completa=fim < hoje,
        semanas_base=[int(s) for s in semanas_base],

        sessoes=cmp("sessoes"),
        km_corrida=cmp("km_corrida", " km"),
        minutos_treino=cmp("minutos_treino", " min"),

        baixa_intensidade=cmp("pct_baixa", "%"),
        gray_zone=cmp("pct_gray", "%"),
        alta_intensidade=cmp("pct_alta", "%"),

        tss=cmp("tss"),
        ctl_fim=_ou_none(atual.get("ctl_fim")),
        atl_fim=_ou_none(atual.get("atl_fim")),
        tsb_fim=_ou_none(atual.get("tsb_fim")),
        ctl_delta=_ou_none(atual.get("ctl_delta")),

        sono_horas=cmp("sono_horas", " h"),
        sono_score=cmp("sono_score"),
        hrv=cmp("hrv", " ms"),
        fc_repouso=cmp("fc_repouso", " bpm"),
        dias_wellness=int(atual.get("dias_wellness") or 0),
    )

    rel.observacoes = _observacoes(rel)
    return rel


def _ou_none(valor):
    """NaN e vazio viram None: no relatório, ausência de dado não é zero."""
    if valor is None or pd.isna(valor):
        return None
    return float(valor)


def _observacoes(rel: RelatorioSemanal) -> list[Observacao]:
    """
    Traduz os números em leituras, cada uma citando o limiar que a disparou.

    A ordem é de severidade: alerta primeiro. Nada aqui é diagnóstico — são
    apontamentos para quem lê decidir, e o limiar vai escrito junto justamente
    para poder ser contestado.
    """
    obs: list[Observacao] = []

    if not rel.completa:
        obs.append(Observacao(
            "atencao",
            "Semana em curso",
            f"Fecha em {rel.fim:%d/%m}. Os totais ainda vão crescer e a "
            "comparação com a base subestima a semana.",
        ))

    if not rel.semanas_base:
        obs.append(Observacao(
            "atencao", "Sem linha de base",
            "Nenhuma semana anterior com treino — os números vêm sem comparação.",
        ))

    # --- carga ------------------------------------------------------------
    if rel.km_corrida.delta_pct is not None and rel.km_corrida.delta_pct > SALTO_VOLUME_PCT:
        obs.append(Observacao(
            "alerta",
            f"Salto de volume: {rel.km_corrida.delta_pct:+.0f}%",
            f"{rel.km_corrida.valor:.1f} km contra base de {rel.km_corrida.base:.1f} km. "
            f"Acima de +{SALTO_VOLUME_PCT:.0f}% o risco de lesão sobe sem ganho proporcional.",
        ))

    if rel.tsb_fim is not None:
        if rel.tsb_fim < TSB_FADIGA:
            obs.append(Observacao(
                "alerta",
                f"Fadiga acumulada (TSB {rel.tsb_fim:+.0f})",
                f"Abaixo de {TSB_FADIGA:.0f} a fadiga recente supera a base de fitness. "
                "Uma semana mais leve tende a converter esse trabalho em forma.",
            ))
        elif rel.tsb_fim > TSB_DESTREINO:
            obs.append(Observacao(
                "atencao",
                f"Forma fresca demais (TSB {rel.tsb_fim:+.0f})",
                f"Acima de {TSB_DESTREINO:.0f} por várias semanas é sinal de estímulo "
                "insuficiente — bom para prova, ruim para construir.",
            ))

    # --- recuperação ------------------------------------------------------
    if rel.hrv.delta_pct is not None and rel.hrv.delta_pct < -QUEDA_HRV_PCT:
        obs.append(Observacao(
            "alerta",
            f"HRV {rel.hrv.delta_pct:+.0f}% abaixo da base",
            f"{rel.hrv.valor:.0f} ms contra {rel.hrv.base:.0f} ms. Queda maior que "
            f"{QUEDA_HRV_PCT:.0f}% costuma aparecer antes do cansaço ser percebido.",
        ))

    if rel.sono_horas.valor is not None and rel.sono_horas.valor < SONO_MINIMO_HORAS:
        obs.append(Observacao(
            "atencao",
            f"Sono médio de {rel.sono_horas.valor:.1f} h",
            f"Abaixo de {SONO_MINIMO_HORAS:.0f} h a adaptação ao treino fica limitada "
            "pela recuperação, não pelo estímulo.",
        ))

    # --- intensidade ------------------------------------------------------
    if rel.gray_zone.valor is not None and rel.gray_zone.valor > LIMITE_GRAY_ZONE:
        obs.append(Observacao(
            "atencao",
            f"Gray zone em {rel.gray_zone.valor:.0f}% do tempo",
            f"Acima de {LIMITE_GRAY_ZONE:.0f}% em Z3 é o padrão 'sempre médio': "
            "cansa como treino forte e rende como treino fácil.",
        ))

    if (
        rel.baixa_intensidade.valor is not None
        and rel.baixa_intensidade.valor < META_BAIXA_INTENSIDADE
    ):
        obs.append(Observacao(
            "atencao",
            f"Base aeróbica em {rel.baixa_intensidade.valor:.0f}% (meta {META_BAIXA_INTENSIDADE:.0f}%)",
            "O modelo polarizado pede a maior parte do volume em Z1+Z2, com o "
            "estímulo forte concentrado em poucas sessões.",
        ))

    if rel.dias_wellness and rel.dias_wellness < 5:
        obs.append(Observacao(
            "atencao",
            f"Só {rel.dias_wellness} dias de wellness na semana",
            "As médias de sono, HRV e FC de repouso vêm de amostra pequena.",
        ))

    # "Nada a apontar" e "nada para avaliar" são coisas diferentes: uma semana
    # anterior ao relógio tem volume e nada mais, e sairia como semana boa.
    fontes_ausentes = []
    if rel.tss.valor is None:
        fontes_ausentes.append("carga de treino")
    if not rel.dias_wellness:
        fontes_ausentes.append("sono/HRV")
    if fontes_ausentes:
        obs.append(Observacao(
            "atencao",
            f"Sem {' e sem '.join(fontes_ausentes)} nesta semana",
            "Os blocos correspondentes ficam vazios — a semana não foi avaliada "
            "nesses aspectos, o que é diferente de ter passado por eles.",
        ))

    if not obs:
        obs.append(Observacao(
            "ok", "Semana sem apontamentos",
            "Volume, intensidade, carga e recuperação dentro dos limiares.",
        ))

    ordem = {"alerta": 0, "atencao": 1, "ok": 2}
    return sorted(obs, key=lambda o: ordem[o.nivel])


# ---------------------------------------------------------------------------
# Apresentação em texto
# ---------------------------------------------------------------------------

MARCADORES = {"alerta": "[!]", "atencao": "[~]", "ok": "[ok]"}


def formatar_relatorio(rel: RelatorioSemanal) -> str:
    """Renderiza o relatório em texto. Uma das apresentações possíveis, não a única."""
    linhas = []
    estado = "" if rel.completa else "  (EM CURSO)"
    linhas.append(f"Semana {rel.semana}: {rel.inicio:%d/%m} a {rel.fim:%d/%m}{estado}")
    if rel.semanas_base:
        linhas.append(
            f"Base: semanas {rel.semanas_base[0]}–{rel.semanas_base[-1]} "
            f"({len(rel.semanas_base)} semanas com treino)"
        )
    linhas.append("=" * 62)

    blocos = [
        ("VOLUME", [
            ("Sessões", rel.sessoes),
            ("Corrida", rel.km_corrida),
            ("Tempo total", rel.minutos_treino),
        ]),
        ("INTENSIDADE (% do tempo de corrida)", [
            ("Z1+Z2 (base)", rel.baixa_intensidade),
            ("Z3 (gray zone)", rel.gray_zone),
            ("Z4+Z5 (forte)", rel.alta_intensidade),
        ]),
        ("RECUPERAÇÃO", [
            ("Sono", rel.sono_horas),
            ("Score de sono", rel.sono_score),
            ("HRV", rel.hrv),
            ("FC de repouso", rel.fc_repouso),
        ]),
    ]

    for titulo, metricas in blocos:
        linhas.append(f"\n{titulo}")
        for rotulo, comparacao in metricas:
            linhas.append(f"  {rotulo:<16} {comparacao}")

    linhas.append("\nCARGA")
    linhas.append(f"  {'TSS da semana':<16} {rel.tss}")
    if rel.ctl_fim is not None:
        variacao = f"{rel.ctl_delta:+.1f} na semana" if rel.ctl_delta is not None else ""
        linhas.append(f"  {'CTL (fitness)':<16} {rel.ctl_fim:.1f}  {variacao}")
        linhas.append(f"  {'ATL (fadiga)':<16} {rel.atl_fim:.1f}")
        linhas.append(f"  {'TSB (forma)':<16} {rel.tsb_fim:+.1f}")

    linhas.append("\nOBSERVAÇÕES")
    for o in rel.observacoes:
        linhas.append(f"  {MARCADORES[o.nivel]} {o.titulo}")
        linhas.append(f"       {o.detalhe}")

    return "\n".join(linhas)
