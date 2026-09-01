"""
Ingestão de dados do Strava.

Lê o activities.json baixado por scripts/strava_export.py e devolve a mesma
dataclass `Activity` usada pelo módulo do Garmin, para que o pipeline trate as
duas fontes de forma idêntica.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ingestion.garmin import Activity


class StravaIngestion:
    """Lê dados do Strava a partir do activities.json (baixado por strava_export.py)."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    # ------------------------------------------------------------------
    # Export do Strava (activities.json)
    # ------------------------------------------------------------------

    def load_export_json(self, json_path: Path | None = None) -> list[Activity]:
        """
        Lê o arquivo de atividades do Strava.

        Diferente do Garmin, o Strava já entrega tudo em unidades do SI
        (metros, segundos, m/s) — não há conversão de escala a fazer.
        """
        if json_path is None:
            candidates = list(self.data_dir.glob("*.json"))
            if not candidates:
                return []
            json_path = candidates[0]

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        # Pode vir como lista pura ou como {"activities": [...]}
        if isinstance(data, dict):
            records = data.get("activities", [])
        else:
            records = data

        activities = []
        for i, record in enumerate(records):
            activity = self._parse_strava_record(record, i)
            if activity:
                activities.append(activity)

        return sorted(activities, key=lambda a: a.start_time)

    def _parse_strava_record(self, record: dict, idx: int) -> Activity | None:
        """Converte um registro do export do Strava em Activity."""
        try:
            start_raw = record.get("start_date") or record.get("start_date_local", "")
            if not start_raw:
                return None

            start_time = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            # Normaliza para datetime naive em UTC — mesma convenção do Garmin,
            # o que permite casar as duas fontes por horário de início
            if start_time.tzinfo:
                start_time = start_time.astimezone(timezone.utc).replace(tzinfo=None)

            distance = float(record.get("distance", 0))  # metros
            # moving_time desconta as pausas; é a base mais justa para o pace
            duration = float(record.get("moving_time") or record.get("elapsed_time", 0))
            elevation = record.get("total_elevation_gain")

            avg_pace = None
            if distance > 0 and duration > 0:
                avg_pace = duration / (distance / 1000)  # s/km

            # Fallback: em treinos sem distância registrada, deriva o pace da
            # velocidade média que o Strava já calculou
            avg_speed = record.get("average_speed")  # m/s
            if avg_pace is None and avg_speed and avg_speed > 0:
                avg_pace = 1000 / avg_speed  # s/km

            return Activity(
                source="strava",
                activity_id=str(record.get("id", idx)),
                name=record.get("name", f"Activity {idx}"),
                sport=record.get("sport_type") or record.get("type", "unknown"),
                start_time=start_time,
                duration_seconds=duration,
                distance_meters=distance,
                avg_heart_rate=record.get("average_heartrate"),
                max_heart_rate=record.get("max_heartrate"),
                avg_pace_sec_per_km=avg_pace,
                elevation_gain_meters=float(elevation) if elevation else None,
                avg_power_watts=record.get("average_watts"),
                # O export resumido do Strava não traz campo de calorias —
                # só o Garmin fornece esse dado. None é proposital.
                calories=None,
                extra=self._strava_extra(record),
            )
        except Exception:
            return None

    @staticmethod
    def _strava_extra(record: dict) -> dict:
        """
        Campos sem lugar na dataclass Activity, achatados para virar coluna de CSV.

        Vale destacar gear_id (permite acompanhar a quilometragem de cada par de
        tênis) e suffer_score (carga percebida calculada pelo Strava), que não
        têm equivalente no export do Garmin.
        """
        start_latlng = record.get("start_latlng") or []
        cadence = record.get("average_cadence")

        extra = {
            "elapsed_time_s": record.get("elapsed_time"),
            "moving_time_s": record.get("moving_time"),
            "avg_speed_mps": record.get("average_speed"),
            "max_speed_mps": record.get("max_speed"),
            # Em esportes de corrida o Strava reporta a cadência por perna;
            # dobrar dá o passos/min real, na mesma escala do Garmin
            "avg_cadence_spm": cadence * 2 if cadence else None,
            "max_power_w": record.get("max_watts"),
            "weighted_avg_power_w": record.get("weighted_average_watts"),
            "kilojoules": record.get("kilojoules"),
            "device_watts": record.get("device_watts"),
            "avg_temperature_c": record.get("average_temp"),
            "elev_high_m": record.get("elev_high"),
            "elev_low_m": record.get("elev_low"),
            "suffer_score": record.get("suffer_score"),
            "device_name": record.get("device_name"),
            "gear_id": record.get("gear_id"),
            "workout_type": record.get("workout_type"),
            "achievement_count": record.get("achievement_count"),
            "pr_count": record.get("pr_count"),
            "kudos_count": record.get("kudos_count"),
            "is_trainer": record.get("trainer"),
            "is_commute": record.get("commute"),
            "is_manual": record.get("manual"),
            "timezone": record.get("timezone"),
            # start_latlng vem como [lat, lng]; separado em duas colunas
            "start_lat": start_latlng[0] if len(start_latlng) == 2 else None,
            "start_lng": start_latlng[1] if len(start_latlng) == 2 else None,
        }
        # Descarta o que veio vazio para não criar colunas só com brancos
        return {k: v for k, v in extra.items() if v is not None}
