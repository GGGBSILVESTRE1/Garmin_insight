"""
Estado da última execução do pipeline.

Existe por causa de uma propriedade desagradável de tarefa agendada: ela falha
em silêncio. O token do Garmin expira, a tarefa não roda, e a interface continua
mostrando os números da semana passada com a mesma confiança de sempre — o que é
pior do que não mostrar nada, porque a decisão de treino é tomada em cima deles.

Então toda execução deixa registro, e a interface lê esse registro antes de
desenhar qualquer número. Dado velho aparece marcado como velho.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from config import ESTADO_EXECUCAO

# Acima disso o dado é considerado velho. Oito dias, e não sete, para uma
# execução semanal que atrasou algumas horas não acender alarme à toa.
DIAS_ATE_ENVELHECER = 8


def registrar(etapas: list[dict], erro: str | None = None) -> dict:
    """
    Grava o resultado de uma execução.

    `etapas` é uma lista de `{"nome", "ok", "detalhe"}` — o registro é por etapa,
    não só do conjunto, porque "sincronizou mas falhou ao reprocessar" e "nem
    conseguiu logar" pedem reações diferentes.
    """
    estado = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "ok": all(e["ok"] for e in etapas) and erro is None,
        "erro": erro,
        "etapas": etapas,
    }

    ESTADO_EXECUCAO.parent.mkdir(parents=True, exist_ok=True)
    with open(ESTADO_EXECUCAO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)

    return estado


def ler() -> dict | None:
    """Último estado gravado, ou None se o pipeline nunca rodou."""
    if not ESTADO_EXECUCAO.exists():
        return None
    with open(ESTADO_EXECUCAO, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return None


def idade() -> timedelta | None:
    """Quanto tempo desde a última execução, ou None se nunca rodou."""
    estado = ler()
    if not estado:
        return None
    try:
        return datetime.now() - datetime.fromisoformat(estado["quando"])
    except (KeyError, ValueError):
        return None


def resumo() -> tuple[str, str]:
    """
    `(nivel, mensagem)` para a interface exibir. Níveis: "ok", "atencao", "erro".

    Nunca devolve "está tudo bem" por omissão: pipeline que nunca rodou e
    pipeline que falhou são estados distintos, e ambos aparecem.
    """
    estado = ler()
    if estado is None:
        return "atencao", "O pipeline nunca rodou aqui — os dados são os que já estavam em disco."

    quando = estado.get("quando", "?")
    decorrido = idade()
    dias = decorrido.days if decorrido else 0

    if not estado.get("ok"):
        falhas = [e["nome"] for e in estado.get("etapas", []) if not e.get("ok")]
        detalhe = f" ({', '.join(falhas)})" if falhas else ""
        return "erro", f"Última execução em {quando[:16]} FALHOU{detalhe}. Dados podem estar velhos."

    if dias >= DIAS_ATE_ENVELHECER:
        return "atencao", f"Última atualização há {dias} dias ({quando[:16]}). A rotina semanal pode ter parado."

    if dias == 0:
        return "ok", f"Atualizado hoje ({quando[11:16]})."
    return "ok", f"Atualizado há {dias} dia(s), em {quando[:16]}."
