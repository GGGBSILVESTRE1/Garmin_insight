"""
Processamento: calcula carga de treino e gera os resumos e CSVs de saída.

Duas responsabilidades:
  - TrainingLoadMetrics: modelo de carga (TSS → CTL/ATL/TSB) e resumos semanais
  - build_tables: escreve todas as métricas ingeridas em CSVs normalizados
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timedelta
from pathlib import Path

from config import PROCESSED_DIR
from ingestion.garmin import Activity



@dataclass
class WeeklySummary:
    """Totais de uma semana de treino."""
    week_start: datetime
    total_activities: int
    total_distance_km: float
    total_duration_minutes: float
    avg_heart_rate: float | None
    sports: dict[str, int]          # esporte → quantidade
    longest_activity_km: float
    avg_pace_str: str | None


@dataclass
class TrainingLoadMetrics:
    """
    Modelo simplificado de ATL/CTL/TSB, no estilo do Training Stress Score.

    O TSS original é calculado a partir de potência medida. Sem potência em
    todos os treinos, aqui a FC entra como proxy da intensidade.
    """
    activities: list[Activity]

    def training_stress_score(self, activity: Activity) -> float:
        """
        Estima o TSS de um treino. Usa potência quando existe, senão FC.
        Fórmula: TSS ≈ (horas × intensidade²) × 100

        A intensidade é normalizada por valores de referência fixos (FTP de
        250W, FC máxima de 185) — ajuste-os se o seu perfil for diferente,
        porque eles deslocam a escala inteira de CTL/ATL.
        """
        duration_h = activity.duration_seconds / 3600
        if activity.avg_power_watts and activity.avg_power_watts > 0:
            # Ciclismo: assume FTP de 250W como padrão
            intensity = activity.avg_power_watts / 250
        elif activity.avg_heart_rate:
            # Corrida: normaliza pela FC máxima assumida de 185
            intensity = activity.avg_heart_rate / 185
        else:
            # Sem FC nem potência (ex.: treino manual), estimativa conservadora
            intensity = 0.6

        return round(duration_h * (intensity ** 2) * 100, 1)

    def compute_ctl_atl(
        self,
        window_days: int = 90,
        ctl_days: int = 42,
        atl_days: int = 7,
    ) -> dict:
        """
        Calcula CTL (fitness), ATL (fadiga) e TSB (forma) dia a dia.

        CTL e ATL são médias móveis exponenciais do TSS diário com constantes
        de tempo diferentes: 42 dias reage devagar (condicionamento acumulado),
        7 dias reage rápido (fadiga recente). O TSB é a diferença — positivo
        significa descansado, negativo significa fadiga acumulada.

        Devolve dict data(str) → {tss, ctl, atl, tsb}.
        """
        if not self.activities:
            return {}

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=window_days)

        # Consolida os treinos do dia num único TSS
        daily_tss: dict = defaultdict(float)
        for act in self.activities:
            d = act.start_time.date()
            if d >= start_date:
                daily_tss[d] += self.training_stress_score(act)

        # Fatores de decaimento: α = 1 - e^(-1/τ), padrão TrainingPeaks
        ctl_alpha = 1 - 2.718281828 ** (-1 / ctl_days)   # ≈ 0.0235 para τ=42
        atl_alpha = 1 - 2.718281828 ** (-1 / atl_days)   # ≈ 0.1331 para τ=7

        # Percorre todos os dias do intervalo, inclusive os sem treino: dia
        # parado tem TSS 0 e faz as médias decaírem, que é o efeito desejado
        ctl = 0.0
        atl = 0.0
        results = {}
        current = start_date
        while current <= end_date:
            tss = daily_tss.get(current, 0.0)
            ctl = ctl + (tss - ctl) * ctl_alpha
            atl = atl + (tss - atl) * atl_alpha
            tsb = ctl - atl
            results[str(current)] = {
                "tss": round(tss, 1),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(tsb, 1),
            }
            current += timedelta(days=1)

        return results

    def weekly_summaries(self, n_weeks: int = 8) -> list[WeeklySummary]:
        """Resumos por semana das últimas n_weeks com treino registrado."""
        if not self.activities:
            return []

        # Agrupa por semana ISO — a chave (ano, semana) evita misturar
        # semanas de mesmo número em anos diferentes
        by_week: dict[tuple, list[Activity]] = defaultdict(list)
        for act in self.activities:
            iso = act.start_time.isocalendar()
            key = (iso.year, iso.week)
            by_week[key].append(act)

        # Pega as n_weeks mais recentes
        sorted_weeks = sorted(by_week.keys())[-n_weeks:]
        summaries = []

        for year, week in sorted_weeks:
            acts = by_week[(year, week)]
            week_start = datetime.fromisocalendar(year, week, 1)

            # Distância só de quem tem: musculação entraria como 0 e puxaria
            # a média e o "maior treino" para baixo
            distances = [a.distance_km for a in acts if a.distance_km > 0]
            durations = [a.duration_minutes for a in acts]
            hr_vals = [a.avg_heart_rate for a in acts if a.avg_heart_rate]

            sports: dict[str, int] = defaultdict(int)
            for a in acts:
                sports[a.sport] += 1

            # Pace médio considerando apenas corridas — misturar com pedaladas
            # daria um número sem significado
            running = [a for a in acts if "run" in a.sport.lower()]
            avg_pace_str = None
            if running:
                paces = [a.avg_pace_sec_per_km for a in running if a.avg_pace_sec_per_km]
                if paces:
                    mean_pace = sum(paces) / len(paces)
                    mins = int(mean_pace // 60)
                    secs = int(mean_pace % 60)
                    avg_pace_str = f"{mins}:{secs:02d} /km"

            summaries.append(WeeklySummary(
                week_start=week_start,
                total_activities=len(acts),
                total_distance_km=round(sum(distances), 1),
                total_duration_minutes=round(sum(durations), 0),
                avg_heart_rate=round(sum(hr_vals) / len(hr_vals), 0) if hr_vals else None,
                sports=dict(sports),
                longest_activity_km=round(max(distances), 1) if distances else 0.0,
                avg_pace_str=avg_pace_str,
            ))

        return summaries




# ---------------------------------------------------------------------------
# Exportação para CSV
# ---------------------------------------------------------------------------

# Colunas presentes em toda atividade, em ordem fixa. Os campos específicos de
# cada fonte (Activity.extra) entram depois destas, em ordem alfabética.
_ACTIVITY_CORE_COLUMNS = [
    "date",
    "time",
    "source",
    "activity_id",
    "name",
    "sport",
    "duration_minutes",
    "distance_km",
    "avg_pace_sec_per_km",
    "avg_pace",
    "avg_heart_rate",
    "max_heart_rate",
    "elevation_gain_meters",
    "avg_power_watts",
    "calories",
    "tss",
]

# Chaves de extra que guardam estruturas aninhadas — cada uma vira sua própria
# tabela, já que não cabem numa célula de CSV
_NESTED_EXTRA_KEYS = ("hr_zones_seconds", "power_zones_seconds", "exercise_sets")


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> Path:
    """
    Escreve as linhas num CSV.

    Usa utf-8-sig (UTF-8 com BOM) porque sem o BOM o Excel abre os acentos
    corrompidos. extrasaction="ignore" permite que uma linha traga chaves fora
    da lista de colunas sem quebrar a escrita.
    """
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _round(value, digits: int = 2):
    """Arredonda só o que for número, deixando strings e None intactos."""
    return round(value, digits) if isinstance(value, (int, float)) else value


def _activity_rows(activities: list[Activity]) -> tuple[list[str], list[dict]]:
    """
    Achata as atividades em linhas de CSV, com o TSS calculado por treino.

    As colunas de extra são descobertas percorrendo os dados, não declaradas:
    Garmin e Strava contribuem com conjuntos diferentes de campos, e a união
    deles só é conhecida em tempo de execução.
    """
    load = TrainingLoadMetrics(activities)
    rows = []
    extra_keys: set[str] = set()

    for a in activities:
        row = {
            "date": a.start_time.strftime("%Y-%m-%d"),
            "time": a.start_time.strftime("%H:%M"),
            "source": a.source,
            "activity_id": a.activity_id,
            "name": a.name,
            "sport": a.sport,
            "duration_minutes": _round(a.duration_minutes, 1),
            "distance_km": _round(a.distance_km, 3),
            "avg_pace_sec_per_km": _round(a.avg_pace_sec_per_km, 1),
            "avg_pace": a.avg_pace_str,
            "avg_heart_rate": a.avg_heart_rate,
            "max_heart_rate": a.max_heart_rate,
            "elevation_gain_meters": _round(a.elevation_gain_meters, 1),
            "avg_power_watts": a.avg_power_watts,
            "calories": _round(a.calories, 1),
            "tss": load.training_stress_score(a),
        }
        for key, value in a.extra.items():
            if key in _NESTED_EXTRA_KEYS:
                continue
            row[key] = _round(value)
            extra_keys.add(key)
        rows.append(row)

    return _ACTIVITY_CORE_COLUMNS + sorted(extra_keys), rows


def _zone_rows(activities: list[Activity], extra_key: str) -> list[dict]:
    """
    Tabela de zonas em formato longo: uma linha por atividade por zona.

    Formato longo em vez de uma coluna por zona porque facilita agrupar e
    filtrar depois (ex.: somar o tempo em zona 4 do mês).
    """
    rows = []
    for a in activities:
        zones = a.extra.get(extra_key) or {}
        total = sum(v for v in zones.values() if v)
        for zone, seconds in sorted(zones.items()):
            rows.append({
                "date": a.start_time.strftime("%Y-%m-%d"),
                "activity_id": a.activity_id,
                "source": a.source,
                "name": a.name,
                "sport": a.sport,
                "zone": zone,
                "seconds": _round(seconds, 1),
                "minutes": _round(seconds / 60, 1),
                "pct_of_activity": _round(seconds / total * 100, 1) if total else None,
            })
    return rows


def _strength_rows(activities: list[Activity]) -> list[dict]:
    """Uma linha por exercício em cada sessão de musculação."""
    rows = []
    for a in activities:
        for s in a.extra.get("exercise_sets") or []:
            rows.append({
                "date": a.start_time.strftime("%Y-%m-%d"),
                "activity_id": a.activity_id,
                "name": a.name,
                "exercise": s.get("category"),
                "sets": s.get("sets"),
                "reps": s.get("reps"),
                "volume": s.get("volume"),
                "max_weight": s.get("max_weight"),
                "duration_s": _round(s.get("duration_s"), 1),
            })
    return rows


def _wellness_rows(wellness: dict) -> tuple[list[str], list[dict]]:
    """
    Uma linha por dia, com todos os campos de DailyWellness mais os
    percentuais derivados (que são properties, não campos).

    Lê os campos da dataclass dinamicamente, então uma métrica nova adicionada
    a DailyWellness vira coluna sem precisar mexer aqui.
    """
    if not wellness:
        return [], []

    sample = next(iter(wellness.values()))
    field_names = [f.name for f in dataclass_fields(sample) if f.name != "date"]
    derived = ["sleep_hours", "sleep_deep_pct", "sleep_rem_pct"]
    columns = ["date"] + derived + field_names

    rows = []
    for d in sorted(wellness):
        w = wellness[d]
        row = {"date": d.isoformat()}
        for name in derived:
            row[name] = getattr(w, name)
        for name in field_names:
            row[name] = _round(getattr(w, name))
        rows.append(row)

    return columns, rows


def build_tables(
    activities: list[Activity],
    wellness: dict | None = None,
    output_dir: str | Path | None = None,
    window_days: int = 365,
    n_weeks: int = 52,
) -> dict[str, Path]:
    """
    Escreve todas as métricas ingeridas em CSVs normalizados dentro de output_dir.

    O athlete_context.md é propositalmente resumido — ele só guarda o que cabe
    num Project do claude.ai sem estourar o orçamento de tokens. Estes CSVs são
    o registro completo: tudo que os parsers do Garmin e do Strava extraem, em
    unidades reais, prontos para planilha ou pandas.

    Cada tabela só é escrita se houver dados para ela (não há CSV de potência
    se nenhum treino registrou watts).

    Sem output_dir, escreve em config.PROCESSED_DIR — que é ancorado na raiz do
    projeto, e não no diretório de trabalho. Um caminho relativo passado aqui
    continua sendo resolvido a partir do CWD, como se espera.

    Devolve um mapa {nome_da_tabela: caminho} dos arquivos escritos.
    """
    out = Path(output_dir) if output_dir is not None else PROCESSED_DIR
    out.mkdir(parents=True, exist_ok=True)
    wellness = wellness or {}
    written: dict[str, Path] = {}

    activities = sorted(activities, key=lambda a: a.start_time)

    if activities:
        columns, rows = _activity_rows(activities)
        written["activities"] = _write_csv(out / "activities.csv", columns, rows)

        # Nas tabelas abaixo as colunas saem das chaves da primeira linha,
        # já que todas as linhas são montadas com o mesmo formato
        hr_rows = _zone_rows(activities, "hr_zones_seconds")
        if hr_rows:
            written["hr_zones"] = _write_csv(
                out / "activity_hr_zones.csv", list(hr_rows[0]), hr_rows
            )

        power_rows = _zone_rows(activities, "power_zones_seconds")
        if power_rows:
            written["power_zones"] = _write_csv(
                out / "activity_power_zones.csv", list(power_rows[0]), power_rows
            )

        strength_rows = _strength_rows(activities)
        if strength_rows:
            written["strength_sets"] = _write_csv(
                out / "strength_sets.csv", list(strength_rows[0]), strength_rows
            )

        load = TrainingLoadMetrics(activities)
        daily = load.compute_ctl_atl(window_days=window_days)
        if daily:
            load_rows = [{"date": d, **vals} for d, vals in sorted(daily.items())]
            written["training_load"] = _write_csv(
                out / "training_load_daily.csv",
                ["date", "tss", "ctl", "atl", "tsb"],
                load_rows,
            )

        weekly = load.weekly_summaries(n_weeks)
        if weekly:
            weekly_rows = [
                {
                    "week_start": w.week_start.strftime("%Y-%m-%d"),
                    "total_activities": w.total_activities,
                    "total_distance_km": w.total_distance_km,
                    "total_duration_minutes": w.total_duration_minutes,
                    "avg_heart_rate": w.avg_heart_rate,
                    "longest_activity_km": w.longest_activity_km,
                    "avg_pace": w.avg_pace_str,
                    # dict de esportes achatado em texto para caber numa célula
                    "sports": "; ".join(f"{s}x{c}" for s, c in sorted(w.sports.items())),
                }
                for w in weekly
            ]
            written["weekly_summary"] = _write_csv(
                out / "weekly_summary.csv", list(weekly_rows[0]), weekly_rows
            )

    wellness_columns, wellness_rows = _wellness_rows(wellness)
    if wellness_rows:
        written["wellness"] = _write_csv(
            out / "wellness_daily.csv", wellness_columns, wellness_rows
        )

    written["manifest"] = _write_manifest(out, written, activities, wellness)
    return written


def _write_manifest(
    out: Path, written: dict[str, Path], activities: list[Activity], wellness: dict
) -> Path:
    """
    Gera o README.md que descreve cada CSV, para a pasta se explicar sozinha.

    Além das descrições fixas, conta as linhas de cada arquivo e resume a
    cobertura dos dados (período, quantidade e origem das atividades).
    """
    descriptions = {
        "activities": "Uma linha por treino (Garmin + Strava), com TSS e todas as métricas específicas de cada fonte.",
        "hr_zones": "Tempo em cada zona de frequência cardíaca, por atividade (formato longo).",
        "power_zones": "Tempo em cada zona de potência, por atividade (formato longo).",
        "strength_sets": "Séries de musculação por exercício: séries, repetições, volume e carga máxima.",
        "training_load": "Série diária de TSS, CTL (fitness), ATL (fadiga) e TSB (forma).",
        "weekly_summary": "Agregado semanal: volume, duração, pace médio e distribuição por esporte.",
        "wellness": "Uma linha por dia: sono e estágios, HRV, FC de repouso, estresse, passos, SpO2 e VO2max.",
    }

    lines = [
        "# Dados processados",
        "",
        ("Gerado por `processing.metrics.build_tables()`. Não editar à mão — "
         "o conteúdo é sobrescrito a cada execução de `scripts/export_context.py`."),
        "",
        "## Arquivos",
        "",
        "| Arquivo | Linhas | Conteúdo |",
        "|---|---|---|",
    ]
    for name, path in written.items():
        if name == "manifest":
            continue
        # conta as linhas do arquivo, descontando o cabeçalho
        with open(path, encoding="utf-8-sig") as f:
            n_rows = sum(1 for _ in f) - 1
        lines.append(f"| `{path.name}` | {n_rows} | {descriptions.get(name, '')} |")

    if activities:
        by_source: dict[str, int] = defaultdict(int)
        for a in activities:
            by_source[a.source] += 1
        span = (
            f"{min(a.start_time for a in activities).date()} a "
            f"{max(a.start_time for a in activities).date()}"
        )
        lines += [
            "",
            "## Cobertura",
            "",
            (f"- **Atividades:** {len(activities)} "
             f"({', '.join(f'{k}: {v}' for k, v in sorted(by_source.items()))})"),
            f"- **Período:** {span}",
        ]
    if wellness:
        lines.append(f"- **Dias de wellness:** {len(wellness)} "
                     f"({min(wellness)} a {max(wellness)})")

    lines += [
        "",
        "## Convenções de unidade",
        "",
        "Valores já convertidos para unidades reais na ingestão:",
        "",
        "- Garmin `distance` e campos de elevação vêm em **centímetros** no export GDPR",
        "- Garmin `duration` e os campos `*TimeInZone_*` vêm em **milissegundos**",
        "- Garmin `avgSpeed`/`maxSpeed` vêm a 1/10 do m/s real",
        "- Garmin `calories`/`bmrCalories` vêm a 10x o valor real",
        "- O export do Strava **não traz calorias** — a coluna só é preenchida em treinos Garmin",
        "",
        f"*Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}*",
    ]

    path = out / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
