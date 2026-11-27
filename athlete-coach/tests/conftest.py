"""Fábricas de dado de teste: atividades e payloads mínimos, sem tocar em dados/."""
from __future__ import annotations

from datetime import datetime

import pytest

from ingestion.garmin import Activity


def atividade(sport="Run", inicio="2026-10-01T10:00:00", dur_s=3600, dist_m=10000,
              fc=150.0, potencia=None, source="garmin", aid="1", **extra) -> Activity:
    return Activity(
        source=source,
        activity_id=str(aid),
        name=f"{sport} {aid}",
        sport=sport,
        start_time=datetime.fromisoformat(inicio),
        duration_seconds=dur_s,
        distance_meters=dist_m,
        avg_heart_rate=fc,
        avg_power_watts=potencia,
        extra=dict(extra),
    )


def resumo(aid, inicio="2026-10-01 10:00:00", tipo="running", nome=None, **campos) -> dict:
    """Resumo de atividade no formato da busca do Connect (só o que o código lê)."""
    return {
        "activityId": int(aid),
        "activityName": nome or f"Atividade {aid}",
        "activityType": {"typeKey": tipo},
        "startTimeGMT": inicio,
        "startTimeLocal": inicio,
        "duration": 3600.0,
        "distance": 10000.0,
        "averageHR": 150.0,
        **campos,
    }


@pytest.fixture
def fabrica_atividade():
    return atividade


@pytest.fixture
def fabrica_resumo():
    return resumo


@pytest.fixture(autouse=True)
def sem_pausa(monkeypatch):
    """O sync dorme 1 s entre chamadas para não irritar a API; nos testes, não."""
    from ingestion import garmin_api

    monkeypatch.setattr(garmin_api, "PAUSA_ENTRE_CHAMADAS", 0)
