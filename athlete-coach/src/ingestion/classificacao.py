"""
Classificação das atividades: categoria, ambiente e flags de qualidade.

Regras determinísticas, não modelo. O `typeKey` do relógio já é o rótulo
("treadmill_running", "strength_training"...), então um classificador só
aprenderia a adivinhar o que o dado já diz. O trabalho aqui é outro: dar um
vocabulário estável para o resto do pipeline e deixar visível o que hoje some
na normalização — a esteira, por exemplo, vira `sport="Run"` igual à rua e só
sobrevivia escondida em `activity_type`.

Três saídas por atividade:

- `categoria`: corrida, musculacao, caminhada, bike, cardio, natacao, outro
- `ambiente`: rua, esteira, pista, trilha, indoor, piscina, aguas_abertas
  (ou None quando não se aplica, ex.: musculação)
- `flags_qualidade`: checagens de qualidade separadas por ";" — o que hoje você só
  descobre abrindo o notebook

As regras têm versão (`VERSAO_REGRAS`). Mudou uma regra, sobe a versão: o
catálogo guarda qual versão classificou cada linha, e dá para saber o que foi
classificado com regra velha.

Uso:
    from ingestion.classificacao import classificar, aplicar
    resultado = classificar(activity)   # Classificacao(categoria, ambiente, flags)
    aplicar(activities)                 # grava em activity.extra, in place
"""
from __future__ import annotations

from dataclasses import dataclass, field

VERSAO_REGRAS = 1

# ---------------------------------------------------------------------------
# Tabelas de regra
# ---------------------------------------------------------------------------

# typeKey do Garmin → (categoria, ambiente). É a fonte mais confiável: vem do
# modo que você escolheu no relógio.
POR_TYPE_KEY: dict[str, tuple[str, str | None]] = {
    "running": ("corrida", "rua"),
    "street_running": ("corrida", "rua"),
    "treadmill_running": ("corrida", "esteira"),
    "indoor_running": ("corrida", "esteira"),
    "track_running": ("corrida", "pista"),
    "trail_running": ("corrida", "trilha"),
    "virtual_run": ("corrida", "esteira"),
    "strength_training": ("musculacao", None),
    "walking": ("caminhada", "rua"),
    "casual_walking": ("caminhada", "rua"),
    "speed_walking": ("caminhada", "rua"),
    "hiking": ("caminhada", "trilha"),
    "cycling": ("bike", "rua"),
    "road_biking": ("bike", "rua"),
    "gravel_cycling": ("bike", "rua"),
    "mountain_biking": ("bike", "trilha"),
    "indoor_cycling": ("bike", "indoor"),
    "virtual_ride": ("bike", "indoor"),
    "lap_swimming": ("natacao", "piscina"),
    "open_water_swimming": ("natacao", "aguas_abertas"),
    "indoor_cardio": ("cardio", "indoor"),
    "fitness_equipment": ("cardio", "indoor"),
    "elliptical": ("cardio", "indoor"),
    "stair_climbing": ("cardio", "indoor"),
    "hiit": ("cardio", "indoor"),
}

# Sport do Strava (atividades sem par no Garmin) → categoria. O Strava não
# distingue esteira no sport; quem diz isso é o campo `trainer` (is_trainer).
POR_SPORT_STRAVA: dict[str, str] = {
    "run": "corrida",
    "trailrun": "corrida",
    "virtualrun": "corrida",
    "weighttraining": "musculacao",
    "walk": "caminhada",
    "hike": "caminhada",
    "ride": "bike",
    "virtualride": "bike",
    "mountainbikeride": "bike",
    "gravelride": "bike",
    "ebikeride": "bike",
    "swim": "natacao",
    "workout": "cardio",
    "crossfit": "cardio",
    "elliptical": "cardio",
    "hiit": "cardio",
}

# Limiares das flags. Julgamento, não fato — por isso nomeados aqui.
DURACAO_MINIMA_S = 5 * 60  # abaixo disso a atividade provavelmente é lixo (start sem querer)
POTENCIA_ESTEIRA_SUSPEITA_W = 450  # potência de corrida em esteira acima disso é artefato
FC_MEDIA_MINIMA = 60  # FC média abaixo disso numa atividade = sensor solto


@dataclass
class Classificacao:
    categoria: str
    ambiente: str | None
    flags: list[str] = field(default_factory=list)

    @property
    def flags_texto(self) -> str:
        """Flags numa célula de CSV: "sem_fc;curta". Vazio quando não há nenhuma."""
        return ";".join(self.flags)


# ---------------------------------------------------------------------------
# Regras
# ---------------------------------------------------------------------------

def _categoria_ambiente(type_key: str | None, sport: str | None,
                        is_trainer: bool | None) -> tuple[str, str | None]:
    """
    Decide categoria e ambiente, do sinal mais forte ao mais fraco:

    1. typeKey do Garmin (modo escolhido no relógio)
    2. sport do Strava + flag `trainer` (o Strava marca esteira/rolo assim)
    3. "outro", sem ambiente
    """
    chave = (type_key or "").strip().lower()
    if chave in POR_TYPE_KEY:
        return POR_TYPE_KEY[chave]

    categoria = POR_SPORT_STRAVA.get((sport or "").strip().lower(), "outro")
    if categoria in ("corrida", "caminhada"):
        return categoria, "esteira" if is_trainer else "rua"
    if categoria == "bike":
        return categoria, "indoor" if is_trainer else "rua"
    if categoria == "natacao":
        return categoria, "piscina"
    if categoria == "cardio":
        return categoria, "indoor"
    return categoria, None


def _flags(activity, categoria: str, ambiente: str | None) -> list[str]:
    """Checagens de qualidade. Cada flag é independente das outras."""
    flags = []
    extra = activity.extra or {}

    if activity.duration_seconds and activity.duration_seconds < DURACAO_MINIMA_S:
        flags.append("curta")

    fc = activity.avg_heart_rate
    if not fc:
        flags.append("sem_fc")
    elif fc < FC_MEDIA_MINIMA:
        flags.append("fc_suspeita")

    # Potência de corrida medida pelo pulso na esteira sai absurda (525 W em
    # 28/09/2026). Não descarta a atividade, só avisa quem for usar a coluna.
    potencia = activity.avg_power_watts
    if (categoria == "corrida" and ambiente == "esteira" and potencia
            and potencia > POTENCIA_ESTEIRA_SUSPEITA_W):
        flags.append("potencia_esteira")

    if categoria == "corrida" and ambiente == "rua" and not activity.distance_meters:
        flags.append("corrida_sem_distancia")

    if categoria == "musculacao":
        series = extra.get("exercise_sets") or []
        if series and all(not s.get("max_weight") for s in series):
            # Limitação do registro: o relógio só sabe a carga se você digitar
            flags.append("series_sem_carga")
        if any(s.get("category") == "UNKNOWN" for s in series):
            flags.append("exercicio_unknown")

    return flags


def classificar(activity) -> Classificacao:
    """
    Classifica uma `Activity` (de qualquer fonte).

    Lê `activity_type` (typeKey do Garmin, presente também nas atividades
    fundidas Strava+Garmin) e `is_trainer` (Strava) de `activity.extra`.
    """
    extra = activity.extra or {}
    categoria, ambiente = _categoria_ambiente(
        extra.get("activity_type"), activity.sport, extra.get("is_trainer")
    )
    return Classificacao(categoria, ambiente, _flags(activity, categoria, ambiente))


def classificar_type_key(type_key: str | None) -> tuple[str, str | None]:
    """Só categoria e ambiente, a partir do typeKey. Usado pelo catálogo no sync."""
    return _categoria_ambiente(type_key, None, None)


def aplicar(activities: list) -> list:
    """
    Grava a classificação em `activity.extra` de cada atividade, in place.

    Vai para `extra` (e não para campos novos da dataclass) porque `extra` já
    vira coluna de CSV sozinho em `build_tables` — `categoria`, `ambiente` e
    `flags_qualidade` aparecem em activities.csv sem mexer no writer. (Não é
    `flags` porque `df.flags` já é um atributo do pandas e esconderia a coluna.)
    """
    for a in activities:
        c = classificar(a)
        a.extra["categoria"] = c.categoria
        if c.ambiente:
            a.extra["ambiente"] = c.ambiente
        else:
            a.extra.pop("ambiente", None)
        if c.flags:
            a.extra["flags_qualidade"] = c.flags_texto
        else:
            a.extra.pop("flags_qualidade", None)
    return activities
