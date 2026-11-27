"""
Cliente da Garmin Connect Personal API (a oficial, liberada em out/2026).

## Ideia central: imitar a superfície do `garminconnect`

`GarminApiIngestion` (em `garmin_api.py`) já sabe sincronizar, cachear e
interpretar tudo — ela só precisa de um objeto com os métodos
`get_sleep_data`, `get_activities_by_date` etc. Este módulo entrega um objeto
com **os mesmos nomes de método**, só que falando com a API oficial. Assim:

- o cache cru continua em `dados/garmin_api/<endpoint>/`
- os parsers continuam os mesmos
- trocar de fonte é trocar o cliente, não o pipeline

Isso só funciona porque as rotas confirmadas da Personal API são as mesmas do
Connect (`/activitylist-service/...`, `/wellness-service/...`), e por isso a
aposta é que os payloads também sejam. **Isso é hipótese até o teste
exploratório confirmar** — `scripts/explorar_personal_api.py` compara campo a
campo com o cache do `garminconnect`.

## O que é confirmado e o que não é (nov/2026)

Confirmado em documentação ou teste público:
- troca de chave por token: `POST .../personal-api-key/token` com
  `Authorization: ApiKey <chave>`; token vale até 24 h, sem refresh token
- busca de atividades: `/activitylist-service/activities/search/activities`
- FIT completo, sono, HRV, FC de repouso, Body Battery

Não confirmado (as rotas abaixo são candidatas, testadas em ordem):
- zonas de FC/potência por atividade, séries de musculação, SpO2
- se as rotas de wellness pedem o displayName no caminho (no Connect pedem)

Relatado como indisponível: VO2max, training status, readiness. Por isso
`ClienteHibrido` manda essas chamadas para o `garminconnect` (fallback).

## Rotas candidatas e memória da que funcionou

Cada método tem uma lista de rotas. A primeira que responde 2xx fica
memorizada para o resto da execução; 404/400 passa para a próxima. Quando o
teste exploratório disser qual é a certa, basta deixar só ela na lista.

Uso:
    from ingestion.garmin_personal import ClientePersonal, ClienteHibrido
    cliente = ClienteHibrido(ClientePersonal.do_ambiente())
    GarminApiIngestion(GARMIN_API_DIR, cliente=cliente).sync()
"""
from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx

BASE_URL = "https://personal.connectapi.garmin.com"
ROTA_TOKEN = "/personalapitoken-service/personal-api/personal-api-key/token"

# Renova o token quando faltar menos que isso para expirar. O token dura 24 h;
# uma margem de 1 h evita que ele expire no meio de um sync longo.
MARGEM_RENOVACAO = timedelta(hours=1)

# Duração assumida quando a resposta do token não diz quando expira
VALIDADE_PADRAO = timedelta(hours=23)

# Pausa entre chamadas e quanto esperar num 429. O termo de uso permite
# throttling sem aviso e não publica limite — devagar é o seguro.
PAUSA_ENTRE_CHAMADAS = 1.0
ESPERA_429 = 30.0

# Página da busca de atividades (o Connect devolve no máximo isso por chamada)
TAMANHO_PAGINA = 20

# Método (mesmo nome do garminconnect) → rotas candidatas, em ordem de tentativa.
# {display_name} só é tentado quando o displayName é conhecido.
ROTAS: dict[str, list[str]] = {
    # confirmada (teste público de 08/11/2026)
    "get_activities_by_date": ["/activitylist-service/activities/search/activities"],
    # candidatas: mesmas rotas do Connect
    "get_activity_hr_in_timezones": ["/activity-service/activity/{aid}/hrTimeInZones"],
    "get_activity_power_in_timezones": ["/activity-service/activity/{aid}/powerTimeInZones"],
    "get_activity_exercise_sets": ["/activity-service/activity/{aid}/exerciseSets"],
    "download_activity_fit": ["/download-service/files/activity/{aid}"],
    "get_sleep_data": [
        "/wellness-service/wellness/dailySleepData",
        "/wellness-service/wellness/dailySleepData/{display_name}",
    ],
    "get_stats": [
        "/usersummary-service/usersummary/daily",
        "/usersummary-service/usersummary/daily/{display_name}",
    ],
    "get_hrv_data": ["/hrv-service/hrv/{data}"],
    "get_spo2_data": ["/wellness-service/wellness/daily/spo2/{data}"],
    # confirmada; não é usada pelo sync, mas serve de teste de fumaça
    "get_daily_heart_rate": ["/wellness-service/wellness/dailyHeartRate"],
    "get_social_profile": ["/userprofile-service/socialProfile"],
}

# Parâmetros de query por método (o Connect usa nomes diferentes para a data)
PARAMS_DATA = {
    "get_sleep_data": lambda d: {"date": d, "nonSleepBufferMinutes": 60},
    "get_stats": lambda d: {"calendarDate": d},
    "get_daily_heart_rate": lambda d: {"date": d},
}

# Relatados como fora da Personal API: vão direto para o fallback
INDISPONIVEIS_NA_PERSONAL = {"get_max_metrics", "get_training_status", "get_training_readiness"}


class IndisponivelError(RuntimeError):
    """O dado não existe nesta fonte (não é falha de rede nem de um dia específico)."""


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, corpo: str = ""):
        super().__init__(f"HTTP {status} em {url}: {corpo[:120]}")
        self.status = status


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def _exp_do_jwt(token: str) -> datetime | None:
    """Lê o `exp` do payload do JWT, sem validar assinatura (só para saber a validade)."""
    partes = token.split(".")
    if len(partes) != 3:
        return None
    try:
        meio = partes[1] + "=" * (-len(partes[1]) % 4)
        dados = json.loads(base64.urlsafe_b64decode(meio))
        return datetime.fromtimestamp(int(dados["exp"]))
    except (ValueError, KeyError, TypeError):
        return None


def escopos_do_jwt(token: str) -> list[str]:
    """Escopos declarados no token — o teste exploratório imprime isso."""
    partes = token.split(".")
    if len(partes) != 3:
        return []
    try:
        meio = partes[1] + "=" * (-len(partes[1]) % 4)
        dados = json.loads(base64.urlsafe_b64decode(meio))
    except ValueError:
        return []
    escopo = dados.get("scope") or dados.get("scopes") or []
    return escopo.split() if isinstance(escopo, str) else list(escopo)


def _ler_token(resposta: dict) -> tuple[str, datetime]:
    """
    Extrai token e validade da resposta da troca de chave.

    O formato exato não está documentado no FAQ, então aceita os nomes mais
    prováveis. Validade: `expires_in` → `exp` do JWT → 23 h assumidas.
    """
    token = (
        resposta.get("access_token")
        or resposta.get("accessToken")
        or resposta.get("token")
    )
    if not token:
        raise RuntimeError(f"resposta do token sem access_token: chaves {sorted(resposta)}")

    expira_em = resposta.get("expires_in") or resposta.get("expiresIn")
    if expira_em:
        validade = datetime.now() + timedelta(seconds=float(expira_em))
    else:
        validade = _exp_do_jwt(token) or datetime.now() + VALIDADE_PADRAO
    return token, validade


# Cabeçalhos onde o token pode vir quando o corpo está vazio
_CABECALHOS_TOKEN = ("authorization", "x-access-token", "access-token", "x-auth-token")


def _parece_token(texto: str) -> bool:
    """Uma linha só, sem espaço, comprida: é token (JWT ou opaco), não HTML nem mensagem."""
    return len(texto) >= 20 and not any(c.isspace() for c in texto) and "<" not in texto


def _interpretar_resposta_token(resposta: httpx.Response) -> tuple[str, datetime]:
    """
    Lê o token da resposta da troca de chave, seja qual for o formato.

    O FAQ não documenta o corpo, e na primeira execução real (27/11/2026) ele
    não veio como JSON. Então aceita, em ordem:

    1. JSON objeto ({"access_token": ...}) → `_ler_token`
    2. JSON string ("eyJ...") ou texto puro com cara de token
    3. corpo vazio com o token num cabeçalho

    Se nada servir, o erro diz status, tipo de conteúdo e o começo do corpo —
    o suficiente para ajustar sem expor o token.
    """
    texto = resposta.text.strip()

    try:
        dados = resposta.json() if texto else None
    except ValueError:
        dados = None

    if isinstance(dados, dict):
        return _ler_token(dados)
    if isinstance(dados, str) and _parece_token(dados.strip()):
        token = dados.strip()
        return token, _exp_do_jwt(token) or datetime.now() + VALIDADE_PADRAO
    if dados is None and _parece_token(texto.strip('"')):
        token = texto.strip('"')
        return token, _exp_do_jwt(token) or datetime.now() + VALIDADE_PADRAO

    for nome in _CABECALHOS_TOKEN:
        valor = resposta.headers.get(nome, "").strip()
        if valor.lower().startswith("bearer "):
            valor = valor[7:].strip()
        if _parece_token(valor):
            return valor, _exp_do_jwt(valor) or datetime.now() + VALIDADE_PADRAO

    raise RuntimeError(
        "Não reconheci a resposta da troca de chave por token: "
        f"HTTP {resposta.status_code}, content-type "
        f"'{resposta.headers.get('content-type', '?')}', {len(resposta.content)} bytes, "
        f"cabeçalhos {sorted(resposta.headers.keys())}, "
        f"início do corpo: {texto[:200]!r}"
        + (f", redireciona para {resposta.headers.get('location')}"
           if 300 <= resposta.status_code < 400 else "")
    )


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

class ClientePersonal:
    """
    Fala com a Personal API e expõe os métodos no formato do `garminconnect`.

    `transporte` existe para teste: um `httpx.MockTransport` roda tudo sem rede.
    """

    def __init__(self, chave: str, cache_token: str | Path | None = None,
                 display_name: str | None = None, transporte=None,
                 pausa: float = PAUSA_ENTRE_CHAMADAS):
        if not chave:
            raise RuntimeError("GARMIN_PERSONAL_API_KEY vazia (veja .env.example)")
        self._chave = chave
        self._cache_token = Path(cache_token) if cache_token else None
        self._display_name = display_name
        self._pausa = pausa
        self._http = httpx.Client(base_url=BASE_URL, timeout=30.0, transport=transporte)
        self._token: str | None = None
        self._validade: datetime | None = None
        self._rota_ok: dict[str, str] = {}  # método → rota que funcionou
        # (método, rota, status) de cada chamada — o teste exploratório lê isso
        self.log: list[tuple[str, str, int]] = []

    @classmethod
    def do_ambiente(cls, **kwargs) -> ClientePersonal:
        """Monta a partir do .env: GARMIN_PERSONAL_API_KEY e, opcional, GARMIN_DISPLAY_NAME."""
        from config import GARMIN_PERSONAL_TOKEN

        return cls(
            chave=os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip(),
            cache_token=kwargs.pop("cache_token", GARMIN_PERSONAL_TOKEN),
            display_name=os.environ.get("GARMIN_DISPLAY_NAME", "").strip() or None,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Token: cache em disco, renovado antes de expirar
    # ------------------------------------------------------------------

    def token(self) -> str:
        if self._token and self._validade and datetime.now() < self._validade - MARGEM_RENOVACAO:
            return self._token

        if self._token is None and self._cache_token and self._cache_token.exists():
            try:
                salvo = json.loads(self._cache_token.read_text(encoding="utf-8"))
                validade = datetime.fromisoformat(salvo["validade"])
                if datetime.now() < validade - MARGEM_RENOVACAO:
                    self._token, self._validade = salvo["token"], validade
                    return self._token
            except (ValueError, KeyError, OSError):
                pass  # cache corrompido: pede token novo

        return self._renovar_token()

    def _renovar_token(self) -> str:
        resposta = self._http.post(ROTA_TOKEN, headers={"Authorization": f"ApiKey {self._chave}"})
        if resposta.status_code in (401, 403):
            raise RuntimeError(
                "A Personal API recusou a chave (HTTP "
                f"{resposta.status_code}). Ela pode ter expirado: gere outra em "
                "Garmin Connect web → Configurações → Personal API Key."
            )
        if resposta.status_code >= 400:
            raise ApiError(resposta.status_code, ROTA_TOKEN, resposta.text)

        self._token, self._validade = _interpretar_resposta_token(resposta)

        if self._cache_token:
            # Token no disco evita trocar a chave a cada execução. Fica em
            # dados/, que nunca vai para o git.
            self._cache_token.parent.mkdir(parents=True, exist_ok=True)
            self._cache_token.write_text(
                json.dumps({"token": self._token, "validade": self._validade.isoformat()}),
                encoding="utf-8",
            )
        return self._token

    # ------------------------------------------------------------------
    # Requisição com rotas candidatas
    # ------------------------------------------------------------------

    def suporta(self, metodo: str) -> bool:
        return metodo in ROTAS and metodo not in INDISPONIVEIS_NA_PERSONAL

    def _rotas(self, metodo: str, **valores) -> list[str]:
        if metodo not in ROTAS:
            raise IndisponivelError(f"{metodo} não existe na Personal API")
        candidatas = [self._rota_ok[metodo]] if metodo in self._rota_ok else ROTAS[metodo]
        rotas = []
        for modelo in candidatas:
            if "{display_name}" in modelo and not self._display_name:
                continue
            rotas.append(modelo.format(display_name=self._display_name, **valores))
        return rotas

    def _get(self, metodo: str, params: dict | None = None, binario: bool = False, **valores):
        """
        GET na primeira rota candidata que responder. 404/400 tenta a próxima;
        401 renova o token uma vez; 429 espera e tenta de novo uma vez.
        Nenhuma rota serviu → IndisponivelError (o sync pula o endpoint).
        """
        ultima: ApiError | None = None
        for rota in self._rotas(metodo, **valores):
            for tentativa in range(2):
                resposta = self._http.get(
                    rota, params=params, headers={"Authorization": f"Bearer {self.token()}"}
                )
                self.log.append((metodo, rota, resposta.status_code))
                if resposta.status_code == 401 and tentativa == 0:
                    self._token = None
                    self._renovar_token()
                    continue
                if resposta.status_code == 429 and tentativa == 0:
                    time.sleep(ESPERA_429)
                    continue
                break

            if resposta.status_code < 300:
                # Memoriza a rota certa (sem os valores) para as próximas chamadas
                for modelo in ROTAS[metodo]:
                    if modelo.format(display_name=self._display_name, **valores) == rota:
                        self._rota_ok[metodo] = modelo
                if self._pausa:
                    time.sleep(self._pausa)
                if binario:
                    return resposta.content
                return resposta.json() if resposta.content else None

            ultima = ApiError(resposta.status_code, rota, resposta.text)
            if resposta.status_code not in (400, 404):
                raise ultima  # erro de verdade (5xx, 403...): não adianta trocar de rota

        raise IndisponivelError(f"{metodo}: nenhuma rota respondeu ({ultima})")

    # ------------------------------------------------------------------
    # Métodos no formato do garminconnect
    # ------------------------------------------------------------------

    def get_activities_by_date(self, inicio: str, fim: str) -> list[dict]:
        """Todas as atividades entre as datas, paginando como o garminconnect faz."""
        todas: list[dict] = []
        start = 0
        while True:
            pagina = self._get(
                "get_activities_by_date",
                params={"startDate": inicio, "endDate": fim,
                        "start": str(start), "limit": str(TAMANHO_PAGINA)},
            ) or []
            todas.extend(pagina)
            if len(pagina) < TAMANHO_PAGINA:
                return todas
            start += TAMANHO_PAGINA

    def get_activity_hr_in_timezones(self, activity_id) -> list:
        return self._get("get_activity_hr_in_timezones", aid=activity_id)

    def get_activity_power_in_timezones(self, activity_id) -> list:
        return self._get("get_activity_power_in_timezones", aid=activity_id)

    def get_activity_exercise_sets(self, activity_id) -> dict:
        return self._get("get_activity_exercise_sets", aid=activity_id)

    def download_activity_fit(self, activity_id) -> bytes:
        """O arquivo original (zip com o .fit dentro), em bytes."""
        return self._get("download_activity_fit", binario=True, aid=activity_id)

    def get_sleep_data(self, data: str) -> dict:
        return self._get("get_sleep_data", params=PARAMS_DATA["get_sleep_data"](data))

    def get_stats(self, data: str) -> dict:
        return self._get("get_stats", params=PARAMS_DATA["get_stats"](data))

    def get_hrv_data(self, data: str) -> dict:
        return self._get("get_hrv_data", data=data)

    def get_spo2_data(self, data: str) -> dict:
        return self._get("get_spo2_data", data=data)

    def get_daily_heart_rate(self, data: str) -> dict:
        return self._get("get_daily_heart_rate", params=PARAMS_DATA["get_daily_heart_rate"](data))

    def get_social_profile(self) -> dict:
        return self._get("get_social_profile")

    def get_max_metrics(self, data: str):
        raise IndisponivelError("VO2max não sai pela Personal API (relatado em nov/2026)")


# ---------------------------------------------------------------------------
# Híbrido: Personal primeiro, garminconnect só para o que falta
# ---------------------------------------------------------------------------

class ClienteHibrido:
    """
    Roteia cada método: Personal API quando ela tem, `garminconnect` quando não.

    O fallback é criado **só quando alguém pede** um método que a Personal não
    tem (hoje, o VO2max). Isso importa porque o login do `garminconnect` pode
    pedir MFA: com a Personal API cobrindo quase tudo, uma sessão expirada do
    não-oficial derruba só o VO2max da semana, não o sync inteiro.

    `fabrica_fallback` devolve o cliente não-oficial (ou levanta RuntimeError).
    """

    def __init__(self, personal: ClientePersonal,
                 fabrica_fallback: Callable[[], object] | None = None):
        self.personal = personal
        self._fabrica = fabrica_fallback
        self._fallback = None
        self._fallback_falhou: str | None = None
        self.origem: dict[str, str] = {}  # método → "personal" | "garminconnect"

    def suporta(self, metodo: str) -> bool:
        return self.personal.suporta(metodo) or self._fabrica is not None

    def _obter_fallback(self):
        if self._fallback is not None:
            return self._fallback
        if self._fallback_falhou:
            raise IndisponivelError(self._fallback_falhou)
        if self._fabrica is None:
            raise IndisponivelError("sem fallback configurado")
        try:
            self._fallback = self._fabrica()
        except RuntimeError as erro:
            # Sessão do não-oficial expirada (MFA) não pode derrubar o resto
            self._fallback_falhou = f"garminconnect indisponível: {erro}"
            raise IndisponivelError(self._fallback_falhou) from erro
        return self._fallback

    def __getattr__(self, metodo: str):
        if metodo.startswith("_"):
            raise AttributeError(metodo)

        if self.personal.suporta(metodo):
            self.origem[metodo] = "personal"
            return getattr(self.personal, metodo)

        def via_fallback(*args, **kwargs):
            self.origem[metodo] = "garminconnect"
            return getattr(self._obter_fallback(), metodo)(*args, **kwargs)

        return via_fallback
