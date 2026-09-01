#!/usr/bin/env python
"""
Exporta todos os dados de treino e wellness para um arquivo Markdown.
Faça upload desse arquivo em um Project no claude.ai para usar como base
de conhecimento do seu treinador pessoal, sem custo de API.

Uso:
    python scripts/export_context.py
    # gera: athlete_context.md
"""

from __future__ import annotations

from datetime import datetime

from config import ATHLETE_CONTEXT, GARMIN_DIR, PROCESSED_DIR, STRAVA_DIR
from ingestion.garmin import GarminIngestion
from ingestion.garmin_wellness import GarminWellnessIngestion
from ingestion.strava import StravaIngestion
from processing.metrics import TrainingLoadMetrics, build_tables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "--"
    m, s = int(sec_per_km // 60), int(sec_per_km % 60)
    return f"{m}:{s:02d}/km"


def _fmt_duration(minutes: float) -> str:
    h, m = int(minutes // 60), int(minutes % 60)
    return f"{h}h{m:02d}m" if h else f"{m}min"


def _sport_label(sport: str) -> str:
    s = sport.lower()
    if any(x in s for x in ("run", "corrida", "treadmill")):
        return "Corrida"
    if any(x in s for x in ("swim", "natacao", "pool")):
        return "Natacao"
    if any(x in s for x in ("ride", "cycling", "bike", "ciclismo")):
        return "Ciclismo"
    if any(x in s for x in ("weight", "strength", "musculacao")):
        return "Musculacao"
    if any(x in s for x in ("fitness", "gym", "training", "workout")):
        return "Academia"
    if "walk" in s:
        return "Caminhada"
    return sport.title()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def section_fitness(activities, wellness) -> str:
    metrics = TrainingLoadMetrics(activities)
    ctl_atl = metrics.compute_ctl_atl(window_days=120)

    latest_date = max(ctl_atl.keys()) if ctl_atl else None
    latest = ctl_atl.get(latest_date, {}) if latest_date else {}

    ctl = latest.get("ctl", 0)
    atl = latest.get("atl", 0)
    tsb = latest.get("tsb", 0)

    # TSB interpretation
    if tsb > 10:
        form_status = "Forma fresca — bom para provas ou treinos intensos"
    elif tsb > 0:
        form_status = "Forma equilibrada — treino normal recomendado"
    elif tsb > -10:
        form_status = "Leve fadiga acumulada — monitore a recuperacao"
    else:
        form_status = "Fadiga elevada — priorize recuperacao"

    # VO2max
    vo2 = None
    if wellness:
        vo2_vals = [(d, w.vo2max) for d, w in wellness.items() if w.vo2max]
        if vo2_vals:
            vo2 = vo2_vals[-1][1]

    lines = ["## Metricas de Fitness Atuais\n"]
    lines.append(f"| Metrica | Valor | Interpretacao |")
    lines.append(f"|---|---|---|")
    lines.append(f"| CTL (Fitness) | {ctl:.1f} | Capacidade aerobica acumulada |")
    lines.append(f"| ATL (Fadiga) | {atl:.1f} | Carga recente dos ultimos 7 dias |")
    lines.append(f"| TSB (Forma) | {tsb:+.1f} | {form_status} |")
    if vo2:
        lines.append(f"| VO2max | {vo2:.0f} ml/kg/min | Estimativa Garmin |")

    return "\n".join(lines)


def section_weekly(activities, n_weeks=12) -> str:
    metrics = TrainingLoadMetrics(activities)
    weeks = metrics.weekly_summaries(n_weeks)

    lines = [f"\n## Ultimas {n_weeks} Semanas de Treino\n"]
    lines.append("| Semana | Atividades | Distancia | Duracao | Pace medio | FC media | Esportes |")
    lines.append("|---|---|---|---|---|---|---|")

    for week in reversed(weeks):
        sports = {}
        for name, count in week.sports.items():
            label = _sport_label(name)
            sports[label] = sports.get(label, 0) + count
        sports_str = ", ".join(f"{k} x{v}" for k, v in sorted(sports.items()))

        lines.append(
            f"| {week.week_start.strftime('%d/%m')} "
            f"| {week.total_activities} "
            f"| {week.total_distance_km:.1f} km "
            f"| {_fmt_duration(week.total_duration_minutes)} "
            f"| {week.avg_pace_str or '--'} "
            f"| {f'{week.avg_heart_rate:.0f} bpm' if week.avg_heart_rate else '--'} "
            f"| {sports_str} |"
        )

    return "\n".join(lines)


def section_recent_activities(activities, n=20) -> str:
    recent = sorted(activities, key=lambda a: a.start_time, reverse=True)[:n]

    lines = [f"\n## Ultimas {n} Atividades\n"]
    lines.append("| Data | Atividade | Distancia | Duracao | Pace | FC media | FC max |")
    lines.append("|---|---|---|---|---|---|---|")

    for a in recent:
        sport = _sport_label(a.sport)
        dist = f"{a.distance_km:.2f} km" if a.distance_km > 0 else "--"
        lines.append(
            f"| {a.start_time.strftime('%d/%m/%Y')} "
            f"| {a.name} ({sport}) "
            f"| {dist} "
            f"| {_fmt_duration(a.duration_minutes)} "
            f"| {a.avg_pace_str or '--'} "
            f"| {f'{a.avg_heart_rate:.0f}' if a.avg_heart_rate else '--'} "
            f"| {f'{a.max_heart_rate:.0f}' if a.max_heart_rate else '--'} |"
        )

    return "\n".join(lines)


def section_wellness(wellness: dict, n_days=30) -> str:
    if not wellness:
        return "\n## Dados de Saude e Recuperacao\n\nNenhum dado de wellness disponivel."

    sorted_dates = sorted(wellness.keys())[-n_days:]

    # Baselines
    hrv_vals = [w.hrv for w in wellness.values() if w.hrv]
    rhr_vals = [w.resting_hr for w in wellness.values() if w.resting_hr]
    sleep_vals = [w.sleep_hours for w in wellness.values() if w.sleep_hours]
    stress_vals = [w.avg_stress for w in wellness.values() if w.avg_stress]

    hrv_base = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None
    rhr_base = sum(rhr_vals) / len(rhr_vals) if rhr_vals else None
    sleep_base = sum(sleep_vals) / len(sleep_vals) if sleep_vals else None
    stress_base = sum(stress_vals) / len(stress_vals) if stress_vals else None

    lines = ["\n## Saude e Recuperacao\n"]
    lines.append("### Referencias pessoais (media historica)\n")
    if hrv_base:
        lines.append(f"- **HRV baseline:** {hrv_base:.0f} ms")
    if rhr_base:
        lines.append(f"- **FC de repouso baseline:** {rhr_base:.0f} bpm")
    if sleep_base:
        lines.append(f"- **Sono medio:** {sleep_base:.1f} h")
    if stress_base:
        lines.append(f"- **Estresse medio diario:** {stress_base:.0f}/100")

    lines.append(f"\n### Ultimos {len(sorted_dates)} dias\n")
    lines.append("| Data | Sono | Score sono | HRV | FC repouso | Estresse | Passos |")
    lines.append("|---|---|---|---|---|---|---|")

    for d in reversed(sorted_dates):
        w = wellness[d]
        sleep_str = f"{w.sleep_hours}h" if w.sleep_hours else "--"
        score_str = str(w.sleep_score) if w.sleep_score else "--"
        hrv_str = f"{w.hrv:.0f}" if w.hrv else "--"
        rhr_str = f"{w.resting_hr}" if w.resting_hr else "--"
        stress_str = str(w.avg_stress) if w.avg_stress else "--"
        steps_str = f"{w.steps:,}" if w.steps else "--"

        # Flag alertas
        flags = []
        if w.hrv and hrv_base and w.hrv < hrv_base * 0.85:
            flags.append("HRV baixo")
        if w.sleep_hours and w.sleep_hours < 6:
            flags.append("sono curto")
        if w.avg_stress and w.avg_stress > 70:
            flags.append("alto estresse")
        flag_str = f" ⚠ {', '.join(flags)}" if flags else ""

        lines.append(
            f"| {d.strftime('%d/%m')} "
            f"| {sleep_str} "
            f"| {score_str} "
            f"| {hrv_str} ms "
            f"| {rhr_str} bpm "
            f"| {stress_str} "
            f"| {steps_str}{flag_str} |"
        )

    return "\n".join(lines)


def section_profile(activities, wellness) -> str:
    if not activities:
        return ""

    total = len(activities)
    date_from = min(a.start_time for a in activities).date()
    date_to = max(a.start_time for a in activities).date()
    months = max(1, (date_to - date_from).days / 30)

    # Sport breakdown
    sport_counts: dict[str, int] = {}
    for a in activities:
        label = _sport_label(a.sport)
        sport_counts[label] = sport_counts.get(label, 0) + 1

    top_sports = sorted(sport_counts.items(), key=lambda x: x[1], reverse=True)[:4]

    lines = ["# Perfil do Atleta\n"]
    lines.append(
        f"- **Periodo de dados:** {date_from.strftime('%d/%m/%Y')} a {date_to.strftime('%d/%m/%Y')}"
    )
    lines.append(f"- **Total de atividades:** {total} ({total / months:.1f}/mes em media)")
    lines.append(f"- **Principais esportes:** {', '.join(f'{s} ({c})' for s, c in top_sports)}")

    # Running stats
    runs = [a for a in activities if _sport_label(a.sport) == "Corrida"]
    if runs:
        total_run_km = sum(a.distance_km for a in runs)
        paces = [a.avg_pace_sec_per_km for a in runs if a.avg_pace_sec_per_km]
        best_pace = min(paces) if paces else None
        lines.append(f"- **Total corrido:** {total_run_km:.0f} km")
        if best_pace:
            lines.append(f"- **Melhor pace registrado:** {_fmt_pace(best_pace)}")

    return "\n".join(lines)


def _merge_garmin_into_strava(
    garmin: list, strava: list, time_tol_s: int = 600, dist_tol_m: float = 200
) -> list:
    """Fold each Garmin activity into its synced Strava twin, keeping one row.

    The Garmin watch auto-uploads workouts to Strava, so the same session shows
    up in both exports (same start_time, same distance). Counting both would
    double CTL/ATL/TSB, but simply dropping the Garmin side throws away
    everything Strava never receives: HR/power zone splits, RPE and workout
    feel, running dynamics, body battery cost, and strength-training sets.

    So the match is merged instead — Strava stays the base record (full history,
    refreshed weekly) and the Garmin fields fill in around it. Garmin activities
    with no Strava match are returned as their own entries.
    """
    unmatched = []
    merged = 0
    for g in garmin:
        match = next(
            (
                s for s in strava
                if abs((g.start_time - s.start_time).total_seconds()) <= time_tol_s
                and abs(g.distance_meters - s.distance_meters) <= dist_tol_m
            ),
            None,
        )
        if match is None:
            unmatched.append(g)
            continue

        # Core fields Strava leaves empty — calories only ever come from Garmin.
        for field in ("calories", "avg_heart_rate", "max_heart_rate",
                      "elevation_gain_meters", "avg_power_watts"):
            if getattr(match, field) is None:
                setattr(match, field, getattr(g, field))

        # Garmin-only extras; never clobber a value Strava already reported.
        for key, value in g.extra.items():
            match.extra.setdefault(key, value)
        match.extra["garmin_activity_id"] = g.activity_id
        match.source = "strava+garmin"
        merged += 1

    if merged:
        print(f"  {merged} atividades sincronizadas Garmin->Strava (dados fundidos)")
    if unmatched:
        print(f"  {len(unmatched)} atividades so no Garmin")
    return unmatched


def system_prompt() -> str:
    return """## Instrucoes para o Claude (cole no campo "Instructions" do Project)

Voce e um treinador pessoal de atletismo especializado. Seus dados de treino e saude estao no arquivo anexo a este projeto.

**Seu comportamento:**
- Analise sempre os dados reais antes de responder
- Use metricas concretas (CTL, ATL, TSB, HRV, sono) para embasar suas recomendacoes
- Alerte sobre sinais de sobretreino (HRV baixo, fadiga elevada, sono ruim)
- Responda em portugues
- Seja direto e pratico — recomende treinos especificos quando perguntado
- Cruce os dados de wellness com os de treino (ex: treino pesado ontem + HRV baixo hoje = recuperar)
- Nunca invente dados que nao estao no arquivo

**Voce pode ajudar com qualquer aspecto do treinamento**, incluindo mas nao limitado a:
analise de forma, planejamento semanal, interpretacao de metricas, recomendacoes de recuperacao,
progressao de carga, metas de prova, ajustes de pace, qualidade do sono e impacto no treino,
sinais de sobretreino, e qualquer outra duvida relacionada a performance e saude do atleta.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("Carregando dados...")

    garmin = GarminIngestion(GARMIN_DIR).load_garmin_connect_json()
    strava = StravaIngestion(STRAVA_DIR).load_export_json()
    wellness = GarminWellnessIngestion(GARMIN_DIR).load_all()

    garmin_only = _merge_garmin_into_strava(garmin, strava)
    all_acts = sorted(garmin_only + strava, key=lambda a: a.start_time)
    print(f"  {len(all_acts)} atividades | {len(wellness)} dias de wellness")

    # Monta o documento
    parts = [
        "<!-- Gerado automaticamente por export_context.py -->",
        "<!-- Faca upload deste arquivo em um Project no claude.ai -->",
        "",
        system_prompt(),
        "---",
        "",
        section_profile(all_acts, wellness),
        "",
        section_fitness(all_acts, wellness),
        "",
        section_weekly(all_acts, n_weeks=12),
        "",
        section_recent_activities(all_acts, n=30),
        "",
        section_wellness(wellness, n_days=30),
        "",
        f"\n---\n*Exportado em {datetime.now().strftime('%d/%m/%Y %H:%M')}*",
    ]

    content = "\n".join(parts)
    ATHLETE_CONTEXT.write_text(content, encoding="utf-8")

    size_kb = ATHLETE_CONTEXT.stat().st_size / 1024
    print(f"\nArquivo gerado: {ATHLETE_CONTEXT.name} ({size_kb:.0f} KB)")

    # CSVs completos — o Markdown acima e resumido, estes guardam tudo
    tables = build_tables(all_acts, wellness, output_dir=PROCESSED_DIR)
    print(f"\nCSVs gerados em dados/{PROCESSED_DIR.name}/:")
    for path in sorted(tables.values()):
        print(f"  {path.name}")
    print("\nProximos passos:")
    print("  1. Abra claude.ai -> Projects -> Novo projeto")
    print("  2. Em 'Instructions', cole o bloco de instrucoes do inicio do arquivo")
    print(f"  3. Faca upload de '{ATHLETE_CONTEXT.name}'")
    print("  4. Comece a conversar como treinador!")
    print("\nPara atualizar os dados:")
    print("  python scripts/strava_export.py && python scripts/export_context.py")
    print("  (depois substitua o arquivo no Project)")


if __name__ == "__main__":
    main()
