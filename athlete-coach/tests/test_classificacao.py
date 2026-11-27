from conftest import atividade

from ingestion.classificacao import aplicar, classificar


def test_esteira_separada_da_rua_pelo_type_key():
    assert classificar(atividade(activity_type="treadmill_running")).ambiente == "esteira"
    assert classificar(atividade(activity_type="running")).ambiente == "rua"


def test_strava_sem_garmin_usa_flag_trainer():
    c = classificar(atividade(source="strava", is_trainer=True))
    assert (c.categoria, c.ambiente) == ("corrida", "esteira")


def test_musculacao_e_flags_de_qualidade():
    series = [{"category": "UNKNOWN", "max_weight": None}, {"category": "SQUAT", "max_weight": None}]
    c = classificar(atividade(sport="WeightTraining", dist_m=0, fc=None,
                              activity_type="strength_training", exercise_sets=series))
    assert c.categoria == "musculacao" and c.ambiente is None
    assert set(c.flags) == {"sem_fc", "series_sem_carga", "exercicio_unknown"}


def test_potencia_absurda_na_esteira_vira_flag():
    c = classificar(atividade(potencia=525.0, activity_type="treadmill_running"))
    assert "potencia_esteira" in c.flags


def test_aplicar_grava_em_extra():
    a = atividade(dur_s=120, activity_type="running")
    aplicar([a])
    assert a.extra["categoria"] == "corrida"
    assert a.extra["ambiente"] == "rua"
    assert a.extra["flags"] == "curta"
