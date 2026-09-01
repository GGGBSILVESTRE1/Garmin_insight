"""
Derivações e estatística sobre as tabelas já carregadas.

Aqui ficam as transformações que os notebooks repetiriam célula a célula:
coluna de semana, lags diários, auditoria de nulos e correlação com p-valor.
O carregamento em si vive em `processing.datasets`.
"""

import pandas as pd
import numpy as np
from scipy import stats


def format_week(df, date_col="date", origin=None):
    """
    Adiciona `comeco_semana` (a segunda-feira daquela data) e `semana` (número
    sequencial a partir de `origin`).

    `origin` é o que torna a numeração comparável entre tabelas. Cada CSV cobre
    um período diferente — `activities` começa em 2024, `wellness` só em 2026 —
    então ancorar na data mínima de cada DataFrame faria "semana 1" significar
    coisas diferentes em cada um, e qualquer cruzamento por `semana` sairia
    errado sem dar erro. Os loaders de `datasets` sempre passam a mesma âncora
    (`datasets.week_origin()`).

    Com `origin=None` a âncora volta a ser a data mínima do próprio DataFrame —
    útil para uma tabela isolada, nunca para cruzar duas.

    Modifica e devolve o mesmo DataFrame.
    """
    datas = pd.to_datetime(df[date_col])

    df["comeco_semana"] = datas - pd.to_timedelta(datas.dt.weekday, unit="D")

    ancora = pd.Timestamp(origin) if origin is not None else df["comeco_semana"].min()
    df["semana"] = (df["comeco_semana"] - ancora).dt.days // 7 + 1

    return df


def add_lags(df, cols, n=1, date_col="date"):
    """
    Cria `<col>_lag<n>` com o valor de n dias antes.

    `.shift()` desloca linhas, não dias: só equivale a "ontem" se o DataFrame
    estiver ordenado por data e sem buracos no calendário. Por isso ordena antes
    e avisa quando há dias faltando — quem garante o calendário completo é
    `datasets.load_daily()`.
    """
    df = df.sort_values(date_col).reset_index(drop=True)

    intervalos = df[date_col].diff().dropna().dt.days
    if not intervalos.empty and (intervalos != 1).any():
        buracos = int((intervalos != 1).sum())
        print(
            f"[add_lags] atenção: {buracos} salto(s) no calendário — "
            f"lag{n} não é literalmente '{n} dia(s) antes' nessas linhas."
        )

    for col in cols:
        df[f"{col}_lag{n}"] = df[col].shift(n)

    return df


def auditar_nulos(df):
    """
    Colunas com valores faltantes, da pior para a melhor.

    Devolve um DataFrame (`coluna`, `faltantes`, `pct`) em vez de imprimir, para
    poder ser filtrado e ordenado no notebook como qualquer outra tabela.
    """
    nulos = df.isnull().sum()
    nulos = nulos[nulos > 0].sort_values(ascending=False)

    return pd.DataFrame(
        {
            "coluna": nulos.index,
            "faltantes": nulos.to_numpy(),
            "pct": (nulos / len(df) * 100).round(1).to_numpy(),
        }
    ).reset_index(drop=True)


def corr_com_pvalor(df, x, y, min_n=10):
    """
    Correlação de Pearson entre duas colunas, ignorando linhas com nulo.

    Devolve `None` abaixo de `min_n` pares — com poucos pontos o r é ruído e o
    p-valor não sustenta leitura nenhuma.
    """
    sub = df[[x, y]].dropna()
    if len(sub) < min_n:
        return None

    r_p, p_p = stats.pearsonr(sub[x], sub[y])
    r_s, p_s = stats.spearmanr(sub[x], sub[y])  # Spearman é mais robusto a outliers
    return {"n": len(sub), "r_pearson": r_p, "p_pearson": p_p, "p_spearman": p_s, "r_spearman": r_s}


def tabela_correlacoes(df, pares, min_n=10):
    """
    Roda correlações Pearson e Spearman sobre uma lista de pares (x, y).
    Ordena pela força de Spearman (mais robusto a não-linearidades).
    Coluna `divergencia` sinaliza pares onde os métodos discordam
    substancialmente — candidatos a relação não-linear.
    """
    linhas = []
    for x, y in pares:
        r = corr_com_pvalor(df, x, y, min_n=min_n)
        if r is None:
            linhas.append({"x": x, "y": y, "n": 0})
            continue
        linhas.append(
            {
                "x": x,
                "y": y,
                "n": r["n"],
                "r_pearson": round(r["r_pearson"], 3),
                "p_pearson": round(r["p_pearson"], 4),
                "r_spearman": round(r["r_spearman"], 3),
                "p_spearman": round(r["p_spearman"], 4),
                "sig_pearson": r["p_pearson"] < 0.05,
                "sig_spearman": r["p_spearman"] < 0.05,
                "divergencia": abs(r["r_spearman"]) - abs(r["r_pearson"]) > 0.1,
            }
        )

    tabela = pd.DataFrame(linhas)
    return tabela.reindex(
        tabela["r_spearman"].abs().sort_values(ascending=False).index
    ).reset_index(drop=True)
