"""
Histórico — a mesma agregação semanal, olhada ao longo do tempo.

A página principal responde "como foi esta semana". Esta responde "para onde
isso está indo", que é outra pergunta e por isso outra página. Os números são os
mesmos: tudo sai de `weekly.agregados_semanais()`, sem recálculo paralelo.
"""
from __future__ import annotations

import streamlit as st

from processing.weekly import agregados_semanais, semanas_com_treino

st.set_page_config(page_title="Histórico", page_icon="📈", layout="wide")


@st.cache_data(ttl=300)
def carregar():
    tabela = agregados_semanais()
    # Só semanas com treino: as outras entram no índice por causa da carga
    # diária (que existe todo dia, mesmo sem atividade) e virariam buracos no
    # gráfico, sugerindo queda onde só há ausência de registro.
    return tabela.loc[tabela.index.isin(semanas_com_treino())]


tabela = carregar()

st.title("Histórico semanal")

quantas = st.slider(
    "Semanas exibidas", min_value=6, max_value=min(60, len(tabela)),
    value=min(20, len(tabela)),
)
recorte = tabela.tail(quantas)

periodo = f"{recorte['inicio'].min():%d/%m/%Y} a {recorte['inicio'].max():%d/%m/%Y}"
st.caption(f"Semanas {recorte.index.min()}–{recorte.index.max()} · {periodo}")

st.subheader("Volume de corrida")
st.bar_chart(recorte["km_corrida"], y_label="km", x_label="semana")

st.subheader("Distribuição de intensidade")
st.caption(
    "Percentual do tempo de corrida por faixa. A leitura útil é a proporção entre "
    "as três, não a altura: o total é sempre 100%."
)
intensidade = recorte[["pct_baixa", "pct_gray", "pct_alta"]].rename(
    columns={
        "pct_baixa": "Z1+Z2 (base)",
        "pct_gray": "Z3 (gray zone)",
        "pct_alta": "Z4+Z5 (forte)",
    }
)
st.area_chart(intensidade, y_label="% do tempo", x_label="semana",
              color=["#27ae60", "#f39c12", "#c0392b"])

st.subheader("Fitness, fadiga e forma")
st.caption(
    "CTL e ATL no fim de cada semana. ATL acima de CTL por muito tempo é fadiga "
    "acumulando; TSB muito positivo por muito tempo é estímulo de menos."
)
carga = recorte[["ctl_fim", "atl_fim", "tsb_fim"]].rename(
    columns={"ctl_fim": "CTL (fitness)", "atl_fim": "ATL (fadiga)", "tsb_fim": "TSB (forma)"}
)
st.line_chart(carga, x_label="semana")

if recorte["dias_wellness"].sum():
    st.subheader("Recuperação")
    st.caption("Médias semanais. Semana com poucos dias de registro puxa a média sem avisar — ver a coluna na tabela abaixo.")
    c1, c2 = st.columns(2)
    with c1:
        st.line_chart(recorte[["hrv"]].rename(columns={"hrv": "HRV (ms)"}), x_label="semana")
    with c2:
        st.line_chart(
            recorte[["sono_horas"]].rename(columns={"sono_horas": "Sono (h)"}), x_label="semana"
        )

with st.expander("Tabela completa"):
    st.dataframe(
        recorte.drop(columns=["inicio", "ctl_inicio"], errors="ignore").round(1),
        width="stretch",
    )
