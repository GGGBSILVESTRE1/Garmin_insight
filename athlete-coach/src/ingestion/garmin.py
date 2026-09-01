"""
Ingestão de dados do Garmin.

Suporta três formatos de entrada, do mais completo ao mais simples:
  - arquivos .fit (um por treino, exige a lib fitparse)
  - Activities.csv do Garmin Connect
  - export GDPR em JSON (summarizedActivities.json), que é o usado no projeto

Todos os três produzem a mesma dataclass `Activity`, para que o resto do
pipeline não precise saber de onde o dado veio.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import fitparse
    HAS_FITPARSE = True
except ImportError:
    HAS_FITPARSE = False


# O export GDPR guarda calorias a 10x o valor real, seguindo o padrão dos
# outros floats (distância em cm, velocidade a 1/10 do m/s). Os valores brutos
# dão 39-51 kcal/min, fisiologicamente impossível; ÷10 cai numa faixa plausível.
CALORIE_SCALE = 10


def _scaled(value, divisor: float):
    """
    Divide um campo do export pela sua escala de unidade.

    Tolera None e valores inválidos devolvendo None, para que um campo
    corrompido não derrube o parsing da atividade inteira.
    """
    if value is None:
        return None
    try:
        return float(value) / divisor
    except (TypeError, ValueError):
        return None


@dataclass
class Activity:
    """
    Registro normalizado de um treino, vindo de qualquer fonte.

    Os campos fixos são o denominador comum entre Garmin e Strava. Tudo que é
    específico de uma fonte (zonas de FC, RPE, gear_id...) vai para `extra`,
    que vira coluna de CSV automaticamente em processing.metrics.build_tables.
    """
    source: str                          # "garmin" | "strava" | "strava+garmin"
    activity_id: str
    name: str
    sport: str
    start_time: datetime
    duration_seconds: float
    distance_meters: float
    avg_heart_rate: float | None = None
    max_heart_rate: float | None = None
    avg_pace_sec_per_km: float | None = None
    elevation_gain_meters: float | None = None
    avg_power_watts: float | None = None
    calories: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60

    @property
    def distance_km(self) -> float:
        return self.distance_meters / 1000

    @property
    def avg_pace_str(self) -> str | None:
        """Pace formatado como "5:30 /km" para exibição."""
        if self.avg_pace_sec_per_km is None:
            return None
        mins = int(self.avg_pace_sec_per_km // 60)
        secs = int(self.avg_pace_sec_per_km % 60)
        return f"{mins}:{secs:02d} /km"

    def to_dict(self) -> dict:
        """Versão serializável, com unidades já convertidas para exibição."""
        return {
            "source": self.source,
            "activity_id": self.activity_id,
            "name": self.name,
            "sport": self.sport,
            "start_time": self.start_time.isoformat(),
            "duration_minutes": round(self.duration_minutes, 1),
            "distance_km": round(self.distance_km, 2),
            "avg_heart_rate": self.avg_heart_rate,
            "max_heart_rate": self.max_heart_rate,
            "avg_pace": self.avg_pace_str,
            "elevation_gain_meters": self.elevation_gain_meters,
            "avg_power_watts": self.avg_power_watts,
            "calories": self.calories,
        }


class GarminIngestion:
    """Lê dados do Garmin a partir de arquivos .fit, CSV ou JSON do export GDPR."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    # ------------------------------------------------------------------
    # Arquivos .fit
    # ------------------------------------------------------------------

    def load_fit_file(self, fit_path: Path) -> Activity | None:
        """
        Converte um único arquivo .fit em Activity.

        Um .fit contém centenas de registros por segundo; só interessa aqui a
        mensagem "session", que traz os totais consolidados do treino.
        """
        if not HAS_FITPARSE:
            raise ImportError("Install fitparse: pip install fitparse")

        fitfile = fitparse.FitFile(str(fit_path))
        session = None
        for record in fitfile.get_messages("session"):
            session = {d.name: d.value for d in record}
            break

        if session is None:
            return None

        start_time = session.get("start_time")
        if not isinstance(start_time, datetime):
            return None

        distance = session.get("total_distance") or 0.0  # metros
        duration = session.get("total_elapsed_time") or 0.0  # segundos
        sport = str(session.get("sport", "unknown"))

        avg_pace = None
        if distance > 0 and duration > 0:
            avg_pace = (duration / (distance / 1000))  # s/km

        return Activity(
            source="garmin",
            activity_id=fit_path.stem,
            name=fit_path.stem.replace("_", " ").title(),
            sport=sport,
            start_time=start_time,
            duration_seconds=duration,
            distance_meters=distance,
            avg_heart_rate=session.get("avg_heart_rate"),
            max_heart_rate=session.get("max_heart_rate"),
            avg_pace_sec_per_km=avg_pace,
            elevation_gain_meters=session.get("total_ascent"),
            avg_power_watts=session.get("avg_power"),
            calories=session.get("total_calories"),
        )

    def load_all_fit_files(self) -> list[Activity]:
        """Carrega todos os .fit sob data_dir, ordenados por data."""
        activities = []
        for fit_path in self.data_dir.rglob("*.fit"):
            activity = self.load_fit_file(fit_path)
            if activity:
                activities.append(activity)
        return sorted(activities, key=lambda a: a.start_time)

    # ------------------------------------------------------------------
    # Export CSV do Garmin Connect (Activities.csv)
    # ------------------------------------------------------------------

    def load_garmin_csv(self, csv_path: Path | None = None) -> list[Activity]:
        """
        Lê o CSV exportado pelo Garmin Connect.
        Sem csv_path, usa o primeiro .csv encontrado em data_dir.
        """
        if csv_path is None:
            candidates = list(self.data_dir.glob("*.csv"))
            if not candidates:
                return []
            csv_path = candidates[0]

        activities = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                activity = self._parse_garmin_csv_row(row, i)
                if activity:
                    activities.append(activity)

        return sorted(activities, key=lambda a: a.start_time)

    def _parse_garmin_csv_row(self, row: dict, idx: int) -> Activity | None:
        """
        Converte uma linha do CSV em Activity.

        O CSV do Connect é gerado no idioma da conta, então os cabeçalhos e o
        separador decimal variam. Linhas que não derem para interpretar são
        descartadas (retorna None) em vez de interromper a carga.
        """
        try:
            # Cabeçalhos variam por idioma; tenta as variantes mais comuns
            date_str = row.get("Date") or row.get("Data") or ""
            start_time = datetime.fromisoformat(date_str.replace(" ", "T")) if date_str else None
            if start_time is None:
                return None

            def parse_float(val: str) -> float | None:
                # Aceita vírgula decimal (locale pt-BR)
                val = (val or "").strip().replace(",", ".")
                try:
                    return float(val) if val else None
                except ValueError:
                    return None

            def parse_pace(val: str) -> float | None:
                """Converte pace no formato 'M:SS' para segundos por km."""
                val = (val or "").strip()
                if not val or val == "--":
                    return None
                parts = val.split(":")
                if len(parts) == 2:
                    try:
                        return int(parts[0]) * 60 + int(parts[1])
                    except ValueError:
                        return None
                return None

            distance_raw = parse_float(row.get("Distance", ""))
            distance_m = (distance_raw or 0) * 1000  # o CSV traz km, não cm

            duration_str = row.get("Time", "") or row.get("Elapsed Time", "")
            duration_s = self._hms_to_seconds(duration_str)

            return Activity(
                source="garmin",
                activity_id=str(idx),
                name=row.get("Activity Name") or row.get("Title") or f"Activity {idx}",
                sport=row.get("Activity Type") or "unknown",
                start_time=start_time,
                duration_seconds=duration_s,
                distance_meters=distance_m,
                avg_heart_rate=parse_float(row.get("Avg HR", "")),
                max_heart_rate=parse_float(row.get("Max HR", "")),
                avg_pace_sec_per_km=parse_pace(row.get("Avg Pace", "")),
                elevation_gain_meters=parse_float(row.get("Elev Gain", "")),
                calories=parse_float(row.get("Calories", "")),
            )
        except Exception:
            return None

    @staticmethod
    def _hms_to_seconds(val: str) -> float:
        """Converte 'H:MM:SS' ou 'M:SS' em segundos totais."""
        val = (val or "").strip()
        parts = val.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except ValueError:
            pass
        return 0.0

    # ------------------------------------------------------------------
    # Export GDPR em JSON (summarizedActivities.json) — fonte usada no projeto
    # ------------------------------------------------------------------

    def load_garmin_connect_json(self, json_path: Path | None = None) -> list[Activity]:
        """
        Lê o export GDPR: *_summarizedActivities.json.

        Unidades verificadas empiricamente (o export não as documenta):
          - beginTimestamp : Unix em milissegundos
          - duration       : milissegundos
          - distance       : centímetros (dividir por 100 → metros)
          - avgSpeed       : 1/10 do m/s real; o pace é calculado de dist/dur
        """
        if json_path is None:
            candidates = list(self.data_dir.rglob("*summarizedActivities.json"))
            if not candidates:
                return []
            json_path = candidates[0]

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        # O arquivo pode vir como lista ou como {"summarizedActivitiesExport": [...]}
        if isinstance(data, list):
            records = data[0].get("summarizedActivitiesExport", []) if data else []
        else:
            records = data.get("summarizedActivitiesExport", [])
        activities = []
        for record in records:
            activity = self._parse_connect_json_record(record)
            if activity:
                activities.append(activity)

        return sorted(activities, key=lambda a: a.start_time)

    def _parse_connect_json_record(self, rec: dict) -> Activity | None:
        """Converte uma entrada de summarizedActivitiesExport em Activity."""
        try:
            ts_ms = rec.get("beginTimestamp")
            if ts_ms is None:
                return None
            # O timestamp é UTC; guardamos naive para comparar com o Strava sem fuso
            start_time = datetime.fromtimestamp(float(ts_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)

            duration_s = float(rec.get("duration", 0)) / 1000
            distance_m = float(rec.get("distance", 0)) / 100  # cm → m

            # Pace derivado de distância/duração — mais confiável que avgSpeed
            avg_pace = None
            if distance_m > 0 and duration_s > 0:
                avg_pace = duration_s / (distance_m / 1000)  # s/km

            sport = str(rec.get("sportType") or rec.get("activityType") or "unknown").lower()

            # A elevação também vem em centímetros, como a distância (conferido
            # contra as atividades Strava correspondentes: 2000 → 20 m em todas).
            elevation = rec.get("elevationGain") or rec.get("totalAscent")

            return Activity(
                source="garmin",
                activity_id=str(rec.get("activityId", "")),
                name=rec.get("name") or "Atividade Garmin",
                sport=sport,
                start_time=start_time,
                duration_seconds=duration_s,
                distance_meters=distance_m,
                avg_heart_rate=rec.get("avgHr"),
                max_heart_rate=rec.get("maxHr"),
                avg_pace_sec_per_km=avg_pace,
                elevation_gain_meters=_scaled(elevation, 100),
                avg_power_watts=rec.get("avgPower"),
                calories=_scaled(rec.get("calories"), CALORIE_SCALE),
                extra=self._connect_json_extra(rec),
            )
        except Exception:
            return None

    @staticmethod
    def _connect_json_extra(rec: dict) -> dict:
        """
        Tudo que não tem campo próprio na dataclass Activity, já convertido para
        unidades reais.

        O dicionário é mantido plano (exceto os blocos de zonas e de séries de
        musculação, que são listas/dicts e viram tabelas separadas) para que
        build_tables consiga escrever cada chave direto como coluna de CSV.
        Para expor um campo novo do export, basta acrescentá-lo aqui.
        """
        extra: dict = {
            "activity_type": rec.get("activityType"),
            "location_name": rec.get("locationName"),
            "device_manufacturer": rec.get("manufacturer"),
            "lap_count": rec.get("lapCount"),

            # Durações — vêm em ms
            "moving_duration_s": _scaled(rec.get("movingDuration"), 1000),
            "elapsed_duration_s": _scaled(rec.get("elapsedDuration"), 1000),

            # Elevação — em centímetros, como a distância
            "elevation_loss_m": _scaled(rec.get("elevationLoss"), 100),
            "min_elevation_m": _scaled(rec.get("minElevation"), 100),
            "max_elevation_m": _scaled(rec.get("maxElevation"), 100),

            # Velocidade — guardada a 1/10 do valor real em m/s
            "avg_speed_mps": _scaled(rec.get("avgSpeed"), 0.1),
            "max_speed_mps": _scaled(rec.get("maxSpeed"), 0.1),
            "grade_adjusted_speed_mps": _scaled(rec.get("avgGradeAdjustedSpeed"), 0.1),

            # Dinâmica de corrida. avgRunCadence conta só uma perna;
            # avgDoubleCadence é o passos/min real.
            "avg_cadence_spm": rec.get("avgDoubleCadence"),
            "max_cadence_spm": rec.get("maxDoubleCadence"),
            "stride_length_cm": rec.get("avgStrideLength"),
            "vertical_oscillation_cm": rec.get("avgVerticalOscillation"),
            "ground_contact_time_ms": rec.get("avgGroundContactTime"),
            "vertical_ratio_pct": rec.get("avgVerticalRatio"),

            # Potência
            "norm_power_w": rec.get("normPower"),
            "max_power_w": rec.get("maxPower"),

            # Fisiologia e percepção subjetiva do treino
            "vo2max": rec.get("vO2MaxValue"),
            "aerobic_te": rec.get("aerobicTrainingEffect"),
            "anaerobic_te": rec.get("anaerobicTrainingEffect"),
            "training_effect_label": rec.get("trainingEffectLabel"),
            "aerobic_te_message": rec.get("aerobicTrainingEffectMessage"),
            "anaerobic_te_message": rec.get("anaerobicTrainingEffectMessage"),
            "body_battery_delta": rec.get("differenceBodyBattery"),
            "bmr_calories": _scaled(rec.get("bmrCalories"), CALORIE_SCALE),
            # O Garmin grava os dois numa escala 0-100; o RPE volta para 1-10.
            "workout_feel": rec.get("workoutFeel"),
            "workout_rpe": _scaled(rec.get("workoutRpe"), 10),

            # Contribuição para as metas diárias
            "steps": rec.get("steps"),
            "moderate_intensity_minutes": rec.get("moderateIntensityMinutes"),
            "vigorous_intensity_minutes": rec.get("vigorousIntensityMinutes"),

            # Ambiente
            "min_temperature_c": rec.get("minTemperature"),
            "max_temperature_c": rec.get("maxTemperature"),

            # Totais de musculação
            "total_sets": rec.get("totalSets"),
            "active_sets": rec.get("activeSets"),
            "total_reps": rec.get("totalReps"),

            # Marcadores
            "is_pr": rec.get("pr"),
            "is_favorite": rec.get("favorite"),

            # Natação
            "strokes": rec.get("strokes"),
            "avg_swolf": rec.get("avgSwolf"),
            "pool_length": rec.get("poolLength"),
            "avg_stroke_distance": rec.get("avgStrokeDistance"),
            "avg_swim_cadence": rec.get("avgSwimCadence"),

            # Blocos de zonas — vêm em milissegundos, convertidos para segundos.
            # O Garmin emite 7 zonas de FC (0-6) e 6 de potência (0-5).
            "hr_zones_seconds": {
                f"zone_{i}": _scaled(rec.get(f"hrTimeInZone_{i}"), 1000)
                for i in range(7)
                if rec.get(f"hrTimeInZone_{i}") is not None
            },
            "power_zones_seconds": {
                f"zone_{i}": _scaled(rec.get(f"powerTimeInZone_{i}"), 1000)
                for i in range(6)
                if rec.get(f"powerTimeInZone_{i}") is not None
            },
            # Séries de musculação agrupadas por exercício
            "exercise_sets": [
                {
                    "category": s.get("category"),
                    "sets": s.get("sets"),
                    "reps": s.get("reps"),
                    "volume": s.get("volume"),
                    "max_weight": s.get("maxWeight"),
                    "duration_s": _scaled(s.get("duration"), 1000),
                }
                for s in (rec.get("summarizedExerciseSets") or [])
            ],
        }
        # Remove o que veio vazio para não gerar colunas de CSV só com brancos
        return {k: v for k, v in extra.items() if v not in (None, {}, [])}
