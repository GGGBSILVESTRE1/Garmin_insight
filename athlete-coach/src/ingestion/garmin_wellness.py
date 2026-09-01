"""
Ingestão dos dados de saúde e recuperação do Garmin.

Lê sono, estresse diário, FC de repouso, HRV e VO2max do export GDPR. Ao
contrário das atividades, esses dados não têm equivalente no Strava — só
existem aqui.

Cada métrica vem de um arquivo diferente do export, e nenhum deles tem o dia
completo sozinho. Por isso a carga é feita em quatro passes que são fundidos
por data (ver load_all).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass
class DailyWellness:
    """
    Um dia de métricas de saúde, consolidando as quatro fontes do export.

    Todos os campos são opcionais: um dia sem relógio no pulso, ou anterior à
    compra do aparelho, chega aqui quase todo vazio.
    """
    date: date

    # Sono
    sleep_total_seconds: int | None = None
    sleep_deep_seconds: int | None = None
    sleep_rem_seconds: int | None = None
    sleep_light_seconds: int | None = None
    sleep_awake_seconds: int | None = None
    sleep_score: int | None = None          # 0-100
    sleep_feedback: str | None = None       # ex.: "POSITIVE_HIGHLY_RECOVERING"
    avg_sleep_stress: float | None = None
    avg_respiration: float | None = None

    # Frequência cardíaca
    resting_hr: int | None = None
    min_hr: int | None = None
    max_hr: int | None = None

    # Variabilidade cardíaca e oxigenação
    hrv: float | None = None
    spo2: float | None = None

    # Estresse
    avg_stress: int | None = None           # 0-100
    max_stress: int | None = None

    # Atividade geral do dia
    steps: int | None = None
    active_calories: float | None = None
    moderate_intensity_minutes: int | None = None
    vigorous_intensity_minutes: int | None = None

    # Condicionamento
    vo2max: float | None = None

    @property
    def sleep_hours(self) -> float | None:
        if self.sleep_total_seconds is None:
            return None
        return round(self.sleep_total_seconds / 3600, 1)

    @property
    def sleep_deep_pct(self) -> float | None:
        """Percentual de sono profundo — referência saudável fica em torno de 15-20%."""
        if not self.sleep_total_seconds or not self.sleep_deep_seconds:
            return None
        return round(self.sleep_deep_seconds / self.sleep_total_seconds * 100, 0)

    @property
    def sleep_rem_pct(self) -> float | None:
        """Percentual de sono REM — referência saudável fica em torno de 20-25%."""
        if not self.sleep_total_seconds or not self.sleep_rem_seconds:
            return None
        return round(self.sleep_rem_seconds / self.sleep_total_seconds * 100, 0)


class GarminWellnessIngestion:
    """
    Lê os dados de wellness do diretório do export GDPR.

    Recebe a raiz da pasta do Garmin e procura os arquivos recursivamente, já
    que o export espalha as métricas por várias subpastas de DI_CONNECT.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def load_all(self) -> dict[date, DailyWellness]:
        """
        Carrega e funde as quatro fontes de wellness.

        A ordem dos passes importa: _merge só preenche campos ainda vazios,
        então quem carrega primeiro tem prioridade. Sono e UDS vêm antes porque
        são os mais completos; healthStatus entra depois só para complementar
        (ele também traz FC, mas medida de outro jeito).

        Devolve um dict data → DailyWellness, ordenado por data.
        """
        records: dict[date, DailyWellness] = {}

        self._merge(records, self._load_sleep())
        self._merge(records, self._load_uds())
        self._merge(records, self._load_health_status())
        self._merge(records, self._load_vo2max())

        return dict(sorted(records.items()))

    # ------------------------------------------------------------------
    # Sono — estágios, score e respiração
    # ------------------------------------------------------------------

    def _load_sleep(self) -> dict[date, DailyWellness]:
        results = {}
        for path in self.data_dir.rglob("*sleepData.json"):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                cal = entry.get("calendarDate")
                if not cal:
                    continue
                try:
                    d = date.fromisoformat(cal)
                except ValueError:
                    continue

                # O Garmin não grava um total de sono; ele é a soma dos estágios.
                # O tempo acordado fica de fora por não ser sono.
                deep = entry.get("deepSleepSeconds", 0) or 0
                light = entry.get("lightSleepSeconds", 0) or 0
                rem = entry.get("remSleepSeconds", 0) or 0
                awake = entry.get("awakeSleepSeconds", 0) or 0
                total = deep + light + rem

                scores = entry.get("sleepScores") or {}
                overall = scores.get("overallScore") if isinstance(scores, dict) else None
                feedback = scores.get("feedback") if isinstance(scores, dict) else None

                results[d] = DailyWellness(
                    date=d,
                    sleep_total_seconds=total if total > 0 else None,
                    sleep_deep_seconds=deep or None,
                    sleep_rem_seconds=rem or None,
                    sleep_light_seconds=light or None,
                    sleep_awake_seconds=awake or None,
                    sleep_score=overall,
                    sleep_feedback=feedback,
                    avg_sleep_stress=entry.get("avgSleepStress"),
                    avg_respiration=entry.get("averageRespiration"),
                )
        return results

    # ------------------------------------------------------------------
    # UDS — User Daily Summary (passos, estresse, FC de repouso, calorias)
    # ------------------------------------------------------------------

    def _load_uds(self) -> dict[date, DailyWellness]:
        results = {}
        for path in self.data_dir.rglob("UDSFile*.json"):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                cal = entry.get("calendarDate")
                if not cal:
                    continue
                try:
                    d = date.fromisoformat(cal)
                except ValueError:
                    continue

                # O estresse vem numa lista de agregadores (dia todo, sono,
                # atividade...); só interessa o de tipo TOTAL
                avg_stress = max_stress = None
                stress_block = entry.get("allDayStress") or {}
                for agg in stress_block.get("aggregatorList", []):
                    if agg.get("type") == "TOTAL":
                        avg_stress = agg.get("averageStressLevel")
                        max_stress = agg.get("maxStressLevel")
                        break

                results[d] = DailyWellness(
                    date=d,
                    resting_hr=entry.get("restingHeartRate"),
                    min_hr=entry.get("minHeartRate"),
                    max_hr=entry.get("maxHeartRate"),
                    steps=entry.get("totalSteps"),
                    active_calories=entry.get("activeKilocalories"),
                    moderate_intensity_minutes=entry.get("moderateIntensityMinutes"),
                    vigorous_intensity_minutes=entry.get("vigorousIntensityMinutes"),
                    avg_stress=avg_stress,
                    max_stress=max_stress,
                )
        return results

    # ------------------------------------------------------------------
    # Health status — HRV, FC matinal e SpO2
    # ------------------------------------------------------------------

    def _load_health_status(self) -> dict[date, DailyWellness]:
        results = {}
        for path in self.data_dir.rglob("*healthStatusData.json"):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                cal = entry.get("calendarDate")
                if not cal:
                    continue
                try:
                    d = date.fromisoformat(cal)
                except ValueError:
                    continue

                # As métricas vêm numa lista genérica {type, value}, não em
                # campos nomeados — daí o loop com despacho por tipo
                hrv = hr = spo2 = None
                for metric in entry.get("metrics", []):
                    t = metric.get("type")
                    v = metric.get("value")
                    if t == "HRV" and v:
                        hrv = float(v)
                    elif t == "HR" and v:
                        hr = float(v)
                    elif t == "SPO2" and v:
                        spo2 = float(v)

                results[d] = DailyWellness(date=d, hrv=hrv, resting_hr=hr, spo2=spo2)
        return results

    # ------------------------------------------------------------------
    # VO2max
    # ------------------------------------------------------------------

    def _load_vo2max(self) -> dict[date, DailyWellness]:
        results = {}
        for path in self.data_dir.rglob("MetricsMaxMetData*.json"):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                cal = entry.get("calendarDate")
                vo2 = entry.get("vo2MaxValue")
                if not cal or not vo2:
                    continue
                try:
                    d = date.fromisoformat(cal)
                except ValueError:
                    continue
                results[d] = DailyWellness(date=d, vo2max=float(vo2))
        return results

    # ------------------------------------------------------------------
    # Fusão
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(base: dict[date, DailyWellness], new: dict[date, DailyWellness]):
        """
        Funde os registros de `new` em `base`, preenchendo apenas campos vazios.

        Percorre os campos via __dataclass_fields__ em vez de listá-los um a um,
        então uma métrica nova adicionada a DailyWellness passa a ser fundida
        automaticamente.
        """
        for d, rec in new.items():
            if d not in base:
                base[d] = rec
            else:
                existing = base[d]
                for field in rec.__dataclass_fields__:
                    if field == "date":
                        continue
                    if getattr(existing, field) is None:
                        val = getattr(rec, field)
                        if val is not None:
                            setattr(existing, field, val)
