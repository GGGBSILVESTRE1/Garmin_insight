"""Cliente da Personal API contra um servidor falso (httpx.MockTransport), sem rede."""
import json

import httpx
import pytest

from ingestion import garmin_personal as gp
from ingestion.garmin_personal import ClienteHibrido, ClientePersonal, IndisponivelError


class ServidorFalso:
    """Responde por rota; conta as trocas de token para testar o cache."""

    def __init__(self, rotas: dict):
        self.rotas = rotas  # caminho → (status, corpo) ou função(request) → (status, corpo)
        self.trocas_de_token = 0
        self.pedidos: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.pedidos.append(caminho)
        if caminho == gp.ROTA_TOKEN:
            assert request.headers["Authorization"] == "ApiKey chave-teste"
            self.trocas_de_token += 1
            return httpx.Response(200, json={"access_token": f"tok{self.trocas_de_token}",
                                             "expires_in": 86400})
        resposta = self.rotas.get(caminho, (404, {"erro": "não existe"}))
        if callable(resposta):
            resposta = resposta(request)
        status, corpo = resposta
        if isinstance(corpo, bytes):
            return httpx.Response(status, content=corpo)
        return httpx.Response(status, json=corpo)


def cliente(servidor, tmp_path, **kw):
    return ClientePersonal("chave-teste", cache_token=tmp_path / "token.json",
                           transporte=httpx.MockTransport(servidor), pausa=0, **kw)


def test_token_trocado_uma_vez_e_cacheado_em_disco(tmp_path):
    srv = ServidorFalso({"/hrv-service/hrv/2026-10-01": (200, {"hrvSummary": {"lastNightAvg": 70}})})
    c = cliente(srv, tmp_path)
    c.get_hrv_data("2026-10-01")
    c.get_hrv_data("2026-10-01")
    assert srv.trocas_de_token == 1
    # Outro processo (próximo sync) reaproveita o token salvo
    cliente(srv, tmp_path).get_hrv_data("2026-10-01")
    assert srv.trocas_de_token == 1
    assert json.loads((tmp_path / "token.json").read_text())["token"] == "tok1"


def test_401_renova_o_token_e_repete(tmp_path):
    estado = {"vezes": 0}

    def hrv(request):
        estado["vezes"] += 1
        return (401, {}) if estado["vezes"] == 1 else (200, {"ok": True})

    srv = ServidorFalso({"/hrv-service/hrv/2026-10-01": hrv})
    assert cliente(srv, tmp_path).get_hrv_data("2026-10-01") == {"ok": True}
    assert srv.trocas_de_token == 2


def test_rota_candidata_404_passa_para_a_proxima_e_memoriza(tmp_path):
    srv = ServidorFalso({
        "/usersummary-service/usersummary/daily/joao": (200, {"restingHeartRate": 50}),
    })
    c = cliente(srv, tmp_path, display_name="joao")
    assert c.get_stats("2026-10-01") == {"restingHeartRate": 50}
    srv.pedidos.clear()
    c.get_stats("2026-10-02")
    # Segunda vez vai direto na rota que funcionou
    assert srv.pedidos == ["/usersummary-service/usersummary/daily/joao"]


def test_sem_rota_que_responda_vira_indisponivel(tmp_path):
    with pytest.raises(IndisponivelError):
        cliente(ServidorFalso({}), tmp_path).get_spo2_data("2026-10-01")


def test_erro_5xx_nao_e_mascarado_como_indisponivel(tmp_path):
    srv = ServidorFalso({"/hrv-service/hrv/2026-10-01": (500, {})})
    with pytest.raises(gp.ApiError):
        cliente(srv, tmp_path).get_hrv_data("2026-10-01")


def test_busca_de_atividades_pagina_ate_acabar(tmp_path):
    def busca(request):
        inicio = int(request.url.params["start"])
        total = 45
        pagina = [{"activityId": i} for i in range(inicio, min(inicio + gp.TAMANHO_PAGINA, total))]
        return 200, pagina

    srv = ServidorFalso({"/activitylist-service/activities/search/activities": busca})
    lista = cliente(srv, tmp_path).get_activities_by_date("2026-10-01", "2026-10-31")
    assert len(lista) == 45


def test_fit_volta_em_bytes(tmp_path):
    srv = ServidorFalso({"/download-service/files/activity/99": (200, b"PK\x03\x04zip")})
    assert cliente(srv, tmp_path).download_activity_fit("99") == b"PK\x03\x04zip"


def test_hibrido_manda_vo2max_para_o_fallback_e_o_resto_para_a_personal(tmp_path):
    class Fallback:
        def get_max_metrics(self, data):
            return {"generic": {"vo2MaxValue": 48}}

    srv = ServidorFalso({"/hrv-service/hrv/2026-10-01": (200, {"x": 1})})
    h = ClienteHibrido(cliente(srv, tmp_path), fabrica_fallback=Fallback)
    assert h.get_hrv_data("2026-10-01") == {"x": 1}
    assert h.get_max_metrics("2026-10-01") == {"generic": {"vo2MaxValue": 48}}
    assert h.origem == {"get_hrv_data": "personal", "get_max_metrics": "garminconnect"}


def test_hibrido_com_fallback_quebrado_so_derruba_o_que_depende_dele(tmp_path):
    def fabrica():
        raise RuntimeError("sessão expirada e o login pede MFA")

    h = ClienteHibrido(cliente(ServidorFalso({}), tmp_path), fabrica_fallback=fabrica)
    with pytest.raises(IndisponivelError):
        h.get_max_metrics("2026-10-01")
    # Segunda chamada não tenta logar de novo
    with pytest.raises(IndisponivelError):
        h.get_max_metrics("2026-10-02")


def test_le_token_sem_expires_in_pelo_exp_do_jwt():
    import base64
    meio = base64.urlsafe_b64encode(json.dumps({"exp": 4102444800, "scope": "A B"}).encode())
    token = f"x.{meio.decode().rstrip('=')}.y"
    valor, validade = gp._ler_token({"accessToken": token})
    assert valor == token and validade.year == 2100
    assert gp.escopos_do_jwt(token) == ["A", "B"]
