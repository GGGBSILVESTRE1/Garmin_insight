"""Fusão Garmin × Strava (1:1, categoria compatível) e build_tables idempotente."""
from conftest import atividade

from ingestion.classificacao import aplicar
from processing.fontes import fundir_garmin_no_strava
from processing.metrics import build_tables


def test_um_strava_nao_absorve_dois_garmin():
    strava = [atividade(source="strava", aid="s1", inicio="2026-10-01T10:00:00")]
    garmin = [
        atividade(aid="g1", inicio="2026-10-01T10:02:00", activity_type="running"),
        atividade(aid="g2", inicio="2026-10-01T10:05:00", activity_type="running"),
    ]
    sem_par = fundir_garmin_no_strava(garmin, strava, verbose=False)
    assert [g.activity_id for g in sem_par] == ["g2"]
    assert strava[0].extra["garmin_activity_id"] == "g1"


def test_musculacao_nao_casa_com_corrida_curta():
    strava = [atividade(source="strava", aid="s1", dist_m=150, inicio="2026-10-01T10:00:00")]
    forca = atividade(aid="g1", sport="WeightTraining", dist_m=0,
                      inicio="2026-10-01T10:03:00", activity_type="strength_training")
    assert fundir_garmin_no_strava([forca], strava, verbose=False) == [forca]


def test_par_mais_proximo_vence():
    strava = [atividade(source="strava", aid="longe", inicio="2026-10-01T10:08:00"),
              atividade(source="strava", aid="perto", inicio="2026-10-01T10:01:00")]
    fundir_garmin_no_strava([atividade(aid="g", inicio="2026-10-01T10:00:00")], strava,
                            verbose=False)
    assert strava[1].source == "strava+garmin" and strava[0].source == "strava"


def test_build_tables_duas_vezes_da_csvs_identicos(tmp_path):
    def montar():
        acts = [
            atividade(aid="1", inicio="2026-10-01T10:00:00", activity_type="running",
                      hr_zones_seconds={"zone_1": 600.0, "zone_2": 3000.0}),
            atividade(aid="2", inicio="2026-10-02T18:00:00", sport="WeightTraining", dist_m=0,
                      activity_type="strength_training",
                      exercise_sets=[{"category": "SQUAT", "sets": 4, "reps": 32}]),
        ]
        return aplicar(acts)

    a, b = tmp_path / "a", tmp_path / "b"
    escritos_a = build_tables(montar(), {}, output_dir=a)
    build_tables(montar(), {}, output_dir=b)
    for nome, caminho in escritos_a.items():
        if nome == "manifest":
            continue  # o README leva a hora da geração
        assert caminho.read_bytes() == (b / caminho.name).read_bytes(), nome
    cabecalho = (a / "activities.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "categoria" in cabecalho and "ambiente" in cabecalho
