"""
Sync de atividades com cliente falso: detecção pelo catálogo, detalhes e FIT
baixados uma vez só, atividade apagada no Connect saindo da análise.
"""
from datetime import date

from conftest import resumo

from ingestion.garmin_api import GarminApiIngestion
from ingestion.garmin_personal import IndisponivelError


class ClienteFalso:
    """Imita o ClienteHibrido: mesmos nomes de método, conta as chamadas."""

    def __init__(self, atividades):
        self.atividades = atividades
        self.personal = object()  # marca a fonte como "personal"
        self.chamadas: dict[str, int] = {}

    def _conta(self, nome):
        self.chamadas[nome] = self.chamadas.get(nome, 0) + 1

    def suporta(self, metodo):
        return metodo != "get_max_metrics"

    def get_activities_by_date(self, inicio, fim):
        self._conta("lista")
        return list(self.atividades)

    def get_activity_hr_in_timezones(self, aid):
        self._conta("hr")
        return [{"zoneNumber": 1, "secsInZone": 600}, {"zoneNumber": 2, "secsInZone": 3000}]

    def get_activity_power_in_timezones(self, aid):
        raise IndisponivelError("sem potência na fonte")

    def get_activity_exercise_sets(self, aid):
        self._conta("series")
        return {"exerciseSets": []}

    def download_activity_fit(self, aid):
        self._conta("fit")
        return b"PK-fit-" + aid.encode()


def _fonte(tmp_path, cliente):
    return GarminApiIngestion(tmp_path / "garmin_api", cliente=cliente,
                              catalogo_path=tmp_path / "catalogo.csv")


def test_segundo_sync_nao_rebaixa_detalhes(tmp_path):
    cliente = ClienteFalso([resumo(1), resumo(2, inicio="2026-10-02 10:00:00")])
    fonte = _fonte(tmp_path, cliente)
    r1 = fonte.sync_activities(desde=date(2026, 10, 1), ate=date(2026, 10, 3), verbose=False)
    assert (r1["novas"], r1["fits"]) == (2, 2)
    assert (tmp_path / "garmin_api" / "fit" / "1.zip").read_bytes() == b"PK-fit-1"

    fonte2 = _fonte(tmp_path, cliente)  # processo novo, catálogo relido do disco
    r2 = fonte2.sync_activities(desde=date(2026, 10, 1), ate=date(2026, 10, 3), verbose=False)
    assert (r2["novas"], r2["alteradas"], r2["detalhes"], r2["fits"]) == (0, 0, 0, 0)
    assert cliente.chamadas["fit"] == 2 and cliente.chamadas["hr"] == 2


def test_potencia_indisponivel_nao_e_perguntada_para_cada_atividade(tmp_path):
    cliente = ClienteFalso([resumo(i, inicio=f"2026-10-0{i} 10:00:00") for i in range(1, 4)])
    r = _fonte(tmp_path, cliente).sync_activities(
        desde=date(2026, 10, 1), ate=date(2026, 10, 5), verbose=False)
    falhas_potencia = [f for f in r["falhas"] if f[0] == "power_zones"]
    assert len(falhas_potencia) == 1


def test_resumo_editado_e_regravado(tmp_path):
    cliente = ClienteFalso([resumo(1)])
    _fonte(tmp_path, cliente).sync_activities(
        desde=date(2026, 10, 1), ate=date(2026, 10, 3), verbose=False)
    cliente.atividades = [resumo(1, nome="Longão de domingo")]
    r = _fonte(tmp_path, cliente).sync_activities(
        desde=date(2026, 10, 1), ate=date(2026, 10, 3), verbose=False)
    assert r["alteradas"] == 1
    assert _fonte(tmp_path, cliente).load_activities()[0].name == "Longão de domingo"


def test_atividade_apagada_no_connect_sai_da_analise_mas_fica_em_disco(tmp_path):
    cliente = ClienteFalso([resumo(1), resumo(2, inicio="2026-10-02 10:00:00")])
    _fonte(tmp_path, cliente).sync_activities(
        desde=date(2026, 10, 1), ate=date(2026, 10, 3), verbose=False)
    cliente.atividades = [resumo(1)]
    r = _fonte(tmp_path, cliente).sync_activities(
        desde=date(2026, 10, 1), ate=date(2026, 10, 3), verbose=False)
    assert r["removidas"] == ["2"]
    assert [a.activity_id for a in _fonte(tmp_path, cliente).load_activities()] == ["1"]
    assert (tmp_path / "garmin_api" / "activities" / "2.json").exists()


def test_marca_dagua_vem_do_catalogo(tmp_path):
    cliente = ClienteFalso([resumo(1, inicio="2026-10-21 10:00:00")])
    _fonte(tmp_path, cliente).sync_activities(
        desde=date(2026, 10, 1), ate=date(2026, 10, 22), verbose=False)
    fonte = _fonte(tmp_path, cliente)
    assert fonte._inicio_atividades(date(2026, 11, 27), fonte._catalogo()) == date(2026, 10, 14)


def test_wellness_pula_endpoint_sem_fonte(tmp_path):
    class Wellness(ClienteFalso):
        def get_sleep_data(self, d): return {"dailySleepDTO": {"deepSleepSeconds": 3600}}
        def get_stats(self, d): return {"restingHeartRate": 50}
        def get_hrv_data(self, d): return {"hrvSummary": {"lastNightAvg": 70}}
        def get_spo2_data(self, d): return {"averageSpO2": 96}

    fonte = _fonte(tmp_path, Wellness([]))
    r = fonte.sync(desde=date(2026, 10, 1), ate=date(2026, 10, 2), verbose=False)
    assert r["indisponiveis"] == ["vo2max"]
    assert r["baixados"] == 8  # 4 endpoints × 2 dias
    assert fonte.load_all()[date(2026, 10, 1)].resting_hr == 50
