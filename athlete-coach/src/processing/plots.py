"""
Peças de gráfico que se repetem entre notebooks.

Só o que seria copiado e colado: a paleta das zonas de FC (para a mesma zona ter
sempre a mesma cor em qualquer gráfico) e o eixo de pace em MM:SS. A montagem
de cada figura continua no notebook, onde dá para ajustar olhando o resultado.
"""

import matplotlib.pyplot as plt
import pandas as pd

# Reexportados de features: a definição das faixas é derivação de dado, não de
# desenho. Ficam disponíveis aqui para os notebooks importarem tudo de um lugar.
from processing.features import FAIXAS_POLARIZADAS, ZONAS_ORDEM  # noqa: F401

ZONAS_CORES = ["#2ecc71", "#3498db", "#f39c12", "#e67e22", "#e74c3c"]

ZONAS_LABELS = [
    "Z1 - Recovery",
    "Z2 - Aeróbico",
    "Z3 - Gray zone",
    "Z4 - Limiar",
    "Z5 - VO2max",
]

FAIXAS_CORES = ["#27ae60", "#f39c12", "#c0392b"]


def sec_para_pace(segundos, _=None) -> str:
    """285 -> '4:45'. Assinatura com o segundo argumento aceita pelo FuncFormatter."""
    if pd.isna(segundos):
        return ""
    return f"{int(segundos // 60)}:{int(segundos % 60):02d}"


def eixo_pace(ax, eixo="y"):
    """
    Formata o eixo em pace MM:SS e o inverte.

    A inversão é o ponto: pace menor é melhor, então sem inverter o gráfico
    mostra "melhorou" para baixo. Invertido, subir continua sendo progresso.
    """
    alvo = ax.yaxis if eixo == "y" else ax.xaxis
    alvo.set_major_formatter(plt.FuncFormatter(sec_para_pace))

    if eixo == "y":
        ax.invert_yaxis()
    else:
        ax.invert_xaxis()

    return ax


def colapsar_faixas(zonas_wide) -> pd.DataFrame:
    """
    Soma as colunas de zona (Z1..Z5, em formato largo) nas três faixas polarizadas.

    Espera o resultado de um `pivot` com uma coluna por zona — tipicamente
    percentual por semana.
    """
    return pd.DataFrame(
        {faixa: zonas_wide[cols].sum(axis=1) for faixa, cols in FAIXAS_POLARIZADAS.items()},
        index=zonas_wide.index,
    )
