"""
Relatório da semana — página principal do app.

Esta camada **não calcula nada**. Todo número vem de `processing.weekly`, o
mesmo módulo que alimenta `scripts/relatorio_semanal.py`. Se uma observação
nova precisa existir, ela nasce lá e aparece aqui de graça — nunca o contrário.
A tela escolhe cor, ordem e forma; a leitura dos dados é do módulo.

Rodar:
    streamlit run app/app.py
"""
from __future__ import annotations

import streamlit as st

from processing.estado import resumo as resumo_estado
from processing.weekly import (
    BASELINE_SEMANAS,
    formatar_relatorio,
    relatorio_semana,
    semanas_com_treino,
    ultima_semana_completa,
)

st.set_page_config(page_title="Relatório semanal", page_icon="🏃", layout="wide")


# --------------------------------------------------------------------------
# Dados
# --------------------------------------------------------------------------
# O cache é por argumento: trocar de semana na barra lateral não relê os CSVs
# inteiros de novo. TTL curto para que uma atualização do pipeline apareça sem
# precisar reiniciar o app.

@st.cache_data(ttl=300)
def carregar_relatorio(semana: int | None, baseline: int, parcial: bool):
    return relatorio_semana(semana=semana, baseline=baseline, parcial=parcial)


@st.cache_data(ttl=300)
def carregar_semanas():
    return semanas_com_treino(), ultima_semana_completa()


# --------------------------------------------------------------------------
# Frescor dos dados — antes de qualquer número
# --------------------------------------------------------------------------
# Vem primeiro de propósito. Rotina agendada falha calada, e um relatório de
# dado velho com cara de novo é pior que nenhum relatório: a decisão de treino
# sai errada sem ninguém desconfiar.

nivel, mensagem = resumo_estado()
{"ok": st.success, "atencao": st.warning, "erro": st.error}[nivel](mensagem, icon=None)


# --------------------------------------------------------------------------
# Seleção
# --------------------------------------------------------------------------

semanas, ultima_completa = carregar_semanas()

with st.sidebar:
    st.header("Semana")

    parcial = st.toggle(
        "Incluir a semana em curso",
        value=False,
        help="A semana atual ainda não fechou: os totais crescem até domingo e a "
             "comparação com a base subestima o que foi feito.",
    )

    opcoes = [s for s in semanas if s <= ultima_completa + (1 if parcial else 0)]
    escolhida = st.selectbox(
        "Qual semana",
        options=list(reversed(opcoes)),
        format_func=lambda s: f"Semana {s}" + (" (em curso)" if s > ultima_completa else ""),
    )

    baseline = st.slider(
        "Semanas na linha de base", min_value=2, max_value=12, value=BASELINE_SEMANAS,
        help="Semanas anteriores com treino usadas para comparar. Semanas sem "
             "treino são puladas, não entram como zero.",
    )

rel = carregar_relatorio(escolhida, baseline, parcial)


# --------------------------------------------------------------------------
# Cabeçalho
# --------------------------------------------------------------------------

titulo = f"Semana {rel.semana} · {rel.inicio:%d/%m} a {rel.fim:%d/%m}"
st.title(titulo + ("  ⏳" if not rel.completa else ""))

if rel.semanas_base:
    st.caption(
        f"Comparada com as semanas {rel.semanas_base[0]}–{rel.semanas_base[-1]} "
        f"({len(rel.semanas_base)} semanas com treino)"
    )


# --------------------------------------------------------------------------
# Observações
# --------------------------------------------------------------------------
# Vêm antes dos números porque são a resposta: os blocos abaixo existem para
# sustentar o que aqui está escrito.

CAIXA = {"alerta": st.error, "atencao": st.warning, "ok": st.success}

st.subheader("O que a semana diz")
for o in rel.observacoes:
    CAIXA[o.nivel](f"**{o.titulo}**  \n{o.detalhe}")


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------

def metrica(coluna, rotulo: str, comparacao, inverso: bool = False, casas: int = 1):
    """
    Um `st.metric` a partir de uma `Comparacao`.

    `inverso=True` para métrica em que menos é melhor (FC de repouso, gray
    zone): sem isso o app pinta de verde uma piora, que é o tipo de erro que
    passa despercebido justamente por parecer certo.

    Percentual compara em pontos percentuais — dizer que 3,9% virou 9,2% é
    "+134%" está certo e é ilegível.
    """
    if comparacao.valor is None:
        coluna.metric(rotulo, "—", help="Sem dado nesta semana")
        return

    valor = f"{comparacao.valor:.{casas}f}{comparacao.unidade}"

    if comparacao.base is None:
        coluna.metric(rotulo, valor)
        return

    delta = (
        f"{comparacao.delta:+.1f} pp"
        if comparacao.unidade == "%"
        else f"{comparacao.delta_pct:+.0f}%"
    )
    coluna.metric(
        rotulo, valor, delta,
        delta_color="inverse" if inverso else "normal",
        help=f"Base: {comparacao.base:.{casas}f}{comparacao.unidade}",
    )


st.subheader("Volume")
c1, c2, c3 = st.columns(3)
metrica(c1, "Sessões", rel.sessoes, casas=0)
metrica(c2, "Corrida", rel.km_corrida)
metrica(c3, "Tempo total", rel.minutos_treino, casas=0)

st.subheader("Intensidade")
st.caption("Percentual do tempo de corrida em cada faixa, excluindo o tempo abaixo de Z1.")
c1, c2, c3 = st.columns(3)
metrica(c1, "Z1+Z2 · base aeróbica", rel.baixa_intensidade)
metrica(c2, "Z3 · gray zone", rel.gray_zone, inverso=True)
metrica(c3, "Z4+Z5 · forte", rel.alta_intensidade)

st.subheader("Carga")
c1, c2, c3, c4 = st.columns(4)
metrica(c1, "TSS da semana", rel.tss, casas=0)
if rel.ctl_fim is not None:
    c2.metric(
        "CTL · fitness", f"{rel.ctl_fim:.1f}",
        f"{rel.ctl_delta:+.1f}" if rel.ctl_delta is not None else None,
        help="Média exponencial longa da carga. Sobe com trabalho consistente.",
    )
    c3.metric("ATL · fadiga", f"{rel.atl_fim:.1f}",
              help="Média exponencial curta. Responde ao que foi feito nos últimos dias.")
    c4.metric("TSB · forma", f"{rel.tsb_fim:+.1f}",
              help="CTL menos ATL. Negativo = carregando fadiga; muito positivo = pouco estímulo.")

st.subheader("Recuperação")
if rel.dias_wellness:
    st.caption(f"Médias sobre {rel.dias_wellness} dia(s) com registro do relógio.")
    c1, c2, c3, c4 = st.columns(4)
    metrica(c1, "Sono", rel.sono_horas)
    metrica(c2, "Score de sono", rel.sono_score, casas=0)
    metrica(c3, "HRV", rel.hrv, casas=0)
    metrica(c4, "FC de repouso", rel.fc_repouso, inverso=True, casas=0)
else:
    st.info("Sem dados de sono/HRV nesta semana — o relógio não registrou ou o período é anterior a ele.")


# --------------------------------------------------------------------------
# Versão em texto
# --------------------------------------------------------------------------
# O mesmo relatório que sai no terminal, para copiar num diário de treino ou
# mandar para alguém. Mesma função, mesma fonte — não é uma segunda versão.

with st.expander("Ver como texto"):
    st.code(formatar_relatorio(rel), language=None)
