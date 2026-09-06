"""
Ingestão de wellness pela API do Garmin Connect — sincronização recorrente.

Complementa `garmin_wellness.py`, que lê o export GDPR: aquele cobre o passado e
depende de um download manual mensal; este mantém a série viva semana a semana.
Os dois produzem `dict[date, DailyWellness]`, então `metrics`, `build_tables`,
os CSVs e os notebooks não sabem — nem precisam saber — de onde veio o dia.

**Não existe API self-service do Garmin para os próprios dados.** A oficial é o
Developer Program, com aprovação por parceria. O caminho usado aqui é a
biblioteca não-oficial `garminconnect` (que roda sobre o `garth`): login com as
credenciais do Connect e sessão salva em disco. Funciona bem para uso pessoal e
quebra quando o Garmin mexe no login — por isso tudo que é específico da
biblioteca está isolado em dois lugares: `ENDPOINTS` e `_obter_cliente()`.

## Buscar e interpretar são etapas separadas

`sync()` fala com a API e grava a resposta **crua** em
`dados/garmin_api/<endpoint>/<AAAA-MM-DD>.json`. `load_all()` lê esses arquivos
e devolve os `DailyWellness`. É a mesma divisão do export GDPR (arquivo no disco
→ parser), e ela paga em três situações: corrigir o parser não exige baixar de
novo, rodar notebook nunca toca a rede, e o dia já baixado continua seu mesmo
que a biblioteca pare de funcionar amanhã.

## Uso

    from config import GARMIN_API_DIR
    from ingestion.garmin_api import GarminApiIngestion

    fonte = GarminApiIngestion(GARMIN_API_DIR)
    fonte.sync()                       # busca o que falta (ver scripts/garmin_sync.py)
    wellness = fonte.load_all()        # lê o cache local, sem rede
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from config import GARMIN_TOKENS
from ingestion.garmin_wellness import DailyWellness, GarminWellnessIngestion

# ---------------------------------------------------------------------------
# Superfície da biblioteca não-oficial
# ---------------------------------------------------------------------------

# Endpoint lógico → método do cliente `garminconnect.Garmin`, chamado com a data
# em ISO. **Este dict é o ponto de ajuste** quando a biblioteca mudar: confira os
# nomes disponíveis na versão instalada com
#     python -c "from garminconnect import Garmin; print([m for m in dir(Garmin) if m.startswith('get_')])"
ENDPOINTS = {
    "sleep": "get_sleep_data",
    "summary": "get_stats",
    "hrv": "get_hrv_data",
    "vo2max": "get_max_metrics",
    "spo2": "get_spo2_data",
}

# Primeira sincronização sem `desde=`: quantos dias para trás buscar. O histórico
# antigo já vem do export GDPR, então o padrão é curto de propósito. Para
# preencher um buraco maior, passe `desde=date(2026, 8, 1)`.
DIAS_PRIMEIRA_SINCRONIZACAO = 60

# Dias recentes que são rebuscados mesmo já estando em cache. O Garmin recalcula
# score de sono e HRV depois do fato; sem isso o dia sincronizado cedo demais
# ficaria congelado no valor provisório.
DIAS_REBUSCA = 3

# Pausa entre chamadas. A API não é pública e não documenta limite — sincronizar
# devagar é o que mantém o acesso funcionando.
PAUSA_ENTRE_CHAMADAS = 1.0


# ---------------------------------------------------------------------------
# Leitura defensiva dos payloads
# ---------------------------------------------------------------------------

def _buscar(payload, *caminhos):
    """
    Primeiro caminho que existir dentro do payload, ou None.

    Cada caminho é uma sequência de chaves. Aceitar vários caminhos para o mesmo
    campo é intencional: as respostas do Connect variam de versão para versão, e
    é mais barato listar os nomes conhecidos do que descobrir na quebra. Uma
    lista no meio do caminho é reduzida ao primeiro item (vários endpoints
    devolvem o dia dentro de um array de um elemento só).
    """
    for caminho in caminhos:
        atual = payload
        for chave in caminho:
            if isinstance(atual, list):
                atual = atual[0] if atual else None
            if not isinstance(atual, dict):
                atual = None
                break
            atual = atual.get(chave)
            if atual is None:
                break
        if atual is not None:
            return atual
    return None


def _inteiro(valor):
    """Converte para int quando dá; devolve None para vazio, texto ou lixo."""
    try:
        return round(float(valor))
    except (TypeError, ValueError):
        return None


def _decimal(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parsers — um por endpoint
# ---------------------------------------------------------------------------

def _parse_sleep(d: date, payload) -> DailyWellness | None:
    profundo = _inteiro(_buscar(payload, ("dailySleepDTO", "deepSleepSeconds")))
    leve = _inteiro(_buscar(payload, ("dailySleepDTO", "lightSleepSeconds")))
    rem = _inteiro(_buscar(payload, ("dailySleepDTO", "remSleepSeconds")))
    acordado = _inteiro(_buscar(payload, ("dailySleepDTO", "awakeSleepSeconds")))

    # O total é somado dos estágios, sem o tempo acordado — mesma definição de
    # garmin_wellness._load_sleep(). Usar o `sleepTimeSeconds` da resposta daria
    # um degrau na série no dia em que a fonte muda do export para a API.
    total = sum(v for v in (profundo, leve, rem) if v) or None

    return DailyWellness(
        date=d,
        sleep_total_seconds=total,
        sleep_deep_seconds=profundo,
        sleep_rem_seconds=rem,
        sleep_light_seconds=leve,
        sleep_awake_seconds=acordado,
        sleep_score=_inteiro(
            _buscar(
                payload,
                ("dailySleepDTO", "sleepScores", "overall", "value"),
                ("dailySleepDTO", "sleepScores", "overallScore"),
            )
        ),
        sleep_feedback=_buscar(
            payload,
            ("dailySleepDTO", "sleepScores", "overall", "qualifierKey"),
            ("dailySleepDTO", "sleepScoreFeedback"),
        ),
        avg_sleep_stress=_decimal(_buscar(payload, ("dailySleepDTO", "avgSleepStress"))),
        avg_respiration=_decimal(
            _buscar(
                payload,
                ("dailySleepDTO", "averageRespirationValue"),
                ("dailySleepDTO", "averageRespiration"),
            )
        ),
    )


def _parse_summary(d: date, payload) -> DailyWellness | None:
    return DailyWellness(
        date=d,
        resting_hr=_inteiro(_buscar(payload, ("restingHeartRate",))),
        min_hr=_inteiro(_buscar(payload, ("minHeartRate",))),
        max_hr=_inteiro(_buscar(payload, ("maxHeartRate",))),
        avg_stress=_inteiro(_buscar(payload, ("averageStressLevel",))),
        max_stress=_inteiro(_buscar(payload, ("maxStressLevel",))),
        steps=_inteiro(_buscar(payload, ("totalSteps",))),
        active_calories=_decimal(_buscar(payload, ("activeKilocalories",))),
        moderate_intensity_minutes=_inteiro(_buscar(payload, ("moderateIntensityMinutes",))),
        vigorous_intensity_minutes=_inteiro(_buscar(payload, ("vigorousIntensityMinutes",))),
    )


def _parse_hrv(d: date, payload) -> DailyWellness | None:
    hrv = _decimal(
        _buscar(
            payload,
            ("hrvSummary", "lastNightAvg"),
            ("hrvSummary", "weeklyAvg"),
            ("lastNightAvg",),
        )
    )
    return DailyWellness(date=d, hrv=hrv) if hrv is not None else None


def _parse_vo2max(d: date, payload) -> DailyWellness | None:
    vo2 = _decimal(
        _buscar(
            payload,
            ("generic", "vo2MaxPreciseValue"),
            ("generic", "vo2MaxValue"),
            ("vo2MaxPreciseValue",),
        )
    )
    return DailyWellness(date=d, vo2max=vo2) if vo2 is not None else None


def _parse_spo2(d: date, payload) -> DailyWellness | None:
    spo2 = _decimal(
        _buscar(payload, ("averageSpO2",), ("averageSpo2",), ("avgSleepSpO2",))
    )
    return DailyWellness(date=d, spo2=spo2) if spo2 is not None else None


# Ordem = precedência na fusão: o primeiro a preencher um campo ganha (ver
# GarminWellnessIngestion._merge). Sono e resumo vêm antes por serem os mais
# completos; os outros três só complementam.
PARSERS = {
    "sleep": _parse_sleep,
    "summary": _parse_summary,
    "hrv": _parse_hrv,
    "vo2max": _parse_vo2max,
    "spo2": _parse_spo2,
}


# ---------------------------------------------------------------------------
# Ingestão
# ---------------------------------------------------------------------------

class GarminApiIngestion:
    """
    Sincroniza wellness da API do Connect e lê o cache local.

    `cliente` existe para teste: passando um objeto com os métodos de
    `ENDPOINTS`, `sync()` roda sem rede e sem credencial.
    """

    def __init__(self, cache_dir: str | Path, cliente=None):
        self.cache_dir = Path(cache_dir)
        self._cliente = cliente

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def load_all(self) -> dict[date, DailyWellness]:
        """
        Lê o cache em disco e devolve `data → DailyWellness`. Não toca a rede.

        Reusa o `_merge` de `GarminWellnessIngestion` de propósito: ele percorre
        os campos via `__dataclass_fields__`, então uma métrica nova adicionada a
        `DailyWellness` passa a ser fundida aqui também, sem edição.
        """
        registros: dict[date, DailyWellness] = {}

        for endpoint, parser in PARSERS.items():
            GarminWellnessIngestion._merge(registros, self._carregar(endpoint, parser))

        return dict(sorted(registros.items()))

    def sync(self, ate: date | None = None, desde: date | None = None,
             rebusca: int = DIAS_REBUSCA, verbose: bool = True) -> dict:
        """
        Baixa os dias que faltam e grava o JSON cru. Devolve um relatório.

        O intervalo termina **ontem**: o dia de hoje ainda está em curso e o
        relógio não fechou sono, estresse nem resumo diário.

        Um dia já em cache é pulado, exceto quando cai numa das duas caudas de
        `rebusca` dias — a do intervalo pedido e a do que já está em cache. As
        duas são necessárias porque o Garmin revisa score de sono e HRV depois
        do fato: numa sincronização semanal, os últimos dias da rodada anterior
        foram gravados ainda provisórios, e sem a segunda cauda eles nunca
        seriam revisitados (na semana seguinte já são velhos demais para a
        primeira).

        Falha de um dia não derruba a sincronização: fica no relatório e os
        outros seguem.
        """
        ate = ate or date.today() - timedelta(days=1)
        cliente = self._obter_cliente()
        limite_rebusca = ate - timedelta(days=rebusca - 1)

        relatorio = {"baixados": 0, "pulados": 0, "falhas": []}

        for endpoint, metodo in ENDPOINTS.items():
            existentes = self._dias_em_cache(endpoint)
            inicio = desde or self._inicio_padrao(existentes, ate, rebusca)

            cauda_cache = (
                max(existentes) - timedelta(days=rebusca - 1) if existentes else None
            )

            for dia in _intervalo(inicio, ate):
                ja_tenho = dia in existentes
                na_cauda = dia >= limite_rebusca or (
                    cauda_cache is not None and dia >= cauda_cache
                )
                if ja_tenho and not na_cauda:
                    relatorio["pulados"] += 1
                    continue

                try:
                    payload = getattr(cliente, metodo)(dia.isoformat())
                except Exception as erro:  # a lib levanta exceções variadas
                    relatorio["falhas"].append((endpoint, dia.isoformat(), str(erro)[:150]))
                    continue

                # Grava mesmo quando a resposta é vazia: registra "já perguntei,
                # não há dado nesse dia" e evita rebuscar o mesmo vazio para sempre.
                self._gravar(endpoint, dia, payload)
                relatorio["baixados"] += 1

                if verbose:
                    print(f"  {endpoint:8} {dia}")
                time.sleep(PAUSA_ENTRE_CHAMADAS)

        return relatorio

    # ------------------------------------------------------------------
    # Atividades
    # ------------------------------------------------------------------

    def load_activities(self) -> list:
        """
        Lê o cache e devolve `list[Activity]` com `source="garmin"`. Sem rede.

        Cada atividade é montada a partir de até quatro arquivos: o resumo e os
        três blocos de detalhe (zonas de FC, zonas de potência, séries). Detalhe
        ausente vira campo vazio, não erro — nem toda atividade tem potência ou
        musculação.
        """
        atividades = []

        for arquivo in sorted(self._pasta("activities").glob("*.json")):
            payload = self._ler_json("activities", arquivo.stem)
            if not isinstance(payload, dict):
                continue

            atividade = _parse_atividade(
                payload,
                zonas_fc=_zonas_para_segundos(self._ler_json("hr_zones", arquivo.stem)),
                zonas_pot=_zonas_para_segundos(self._ler_json("power_zones", arquivo.stem)),
                series=_agrupar_series(self._ler_json("exercise_sets", arquivo.stem)),
            )
            if atividade is not None:
                atividades.append(atividade)

        return sorted(atividades, key=lambda a: a.start_time)

    def sync_activities(self, desde: date | None = None, ate: date | None = None,
                        verbose: bool = True) -> dict:
        """
        Baixa as atividades do período e os detalhes de cada uma.

        A lista vem numa chamada só, então o resumo é sempre regravado — é
        barato e absorve renomeações e correções de esporte feitas depois.
        Já os detalhes (zonas, séries) custam uma chamada por atividade e não
        mudam depois do treino: são buscados **uma vez** e nunca mais.

        `ate` inclui hoje, ao contrário do wellness: uma atividade fica completa
        assim que sobe do relógio, não precisa o dia fechar.
        """
        ate = ate or date.today()
        cliente = self._obter_cliente()
        desde = desde or self._inicio_atividades(ate)

        relatorio = {"atividades": 0, "detalhes": 0, "falhas": []}

        try:
            listar = getattr(cliente, ENDPOINTS_ATIVIDADE["activities"])
            lista = listar(desde.isoformat(), ate.isoformat()) or []
        except Exception as erro:
            relatorio["falhas"].append(("activities", f"{desde}..{ate}", str(erro)[:150]))
            return relatorio

        if verbose:
            print(f"  {len(lista)} atividades entre {desde} e {ate}")

        for atividade in lista:
            if not isinstance(atividade, dict):
                continue
            aid = str(atividade.get("activityId") or "").strip()
            if not aid:
                continue

            self._gravar_json("activities", aid, atividade)
            relatorio["atividades"] += 1

            for chave in ("hr_zones", "power_zones", "exercise_sets"):
                if (self._pasta(chave) / f"{aid}.json").exists():
                    continue
                try:
                    payload = getattr(cliente, ENDPOINTS_ATIVIDADE[chave])(aid)
                except Exception as erro:
                    relatorio["falhas"].append((chave, aid, str(erro)[:150]))
                    continue

                self._gravar_json(chave, aid, payload)
                relatorio["detalhes"] += 1
                time.sleep(PAUSA_ENTRE_CHAMADAS)

            if verbose:
                nome = atividade.get("activityName", "?")
                print(f"    {atividade.get('startTimeLocal', '?')[:10]}  {nome}")

        return relatorio

    def _inicio_atividades(self, ate: date) -> date:
        """
        Retrocede até a última atividade em cache, menos a janela de rebusca.

        A janela é maior que a do wellness porque atividade é editável: nome,
        esporte e até a percepção de esforço podem mudar dias depois do treino.
        """
        ultima = None
        for arquivo in self._pasta("activities").glob("*.json"):
            payload = self._ler_json("activities", arquivo.stem)
            if not isinstance(payload, dict):
                continue
            bruto = payload.get("startTimeGMT") or payload.get("startTimeLocal")
            if not bruto:
                continue
            try:
                dia = datetime.fromisoformat(str(bruto).replace("Z", "").strip()).date()
            except ValueError:
                continue
            if ultima is None or dia > ultima:
                ultima = dia

        if ultima is None:
            return ate - timedelta(days=DIAS_PRIMEIRA_SINCRONIZACAO)
        return ultima - timedelta(days=DIAS_REBUSCA_ATIVIDADES)

    # ------------------------------------------------------------------
    # Cache em disco
    # ------------------------------------------------------------------

    def _pasta(self, endpoint: str) -> Path:
        return self.cache_dir / endpoint

    def _ler_json(self, endpoint: str, nome: str):
        """Conteúdo de um arquivo do cache, ou None se não existir/estiver corrompido."""
        caminho = self._pasta(endpoint) / f"{nome}.json"
        if not caminho.exists():
            return None
        with open(caminho, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return None

    def _gravar_json(self, endpoint: str, nome: str, payload) -> Path:
        pasta = self._pasta(endpoint)
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / f"{nome}.json"
        with open(destino, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return destino

    def _dias_em_cache(self, endpoint: str) -> set[date]:
        dias = set()
        for arquivo in self._pasta(endpoint).glob("*.json"):
            try:
                dias.add(date.fromisoformat(arquivo.stem))
            except ValueError:
                continue  # arquivo fora do padrão de nome: ignora
        return dias

    def _gravar(self, endpoint: str, dia: date, payload) -> Path:
        """Grava um payload de wellness, nomeado pela data."""
        return self._gravar_json(endpoint, dia.isoformat(), payload)

    def _carregar(self, endpoint: str, parser) -> dict[date, DailyWellness]:
        registros = {}
        for arquivo in sorted(self._pasta(endpoint).glob("*.json")):
            try:
                dia = date.fromisoformat(arquivo.stem)
            except ValueError:
                continue

            with open(arquivo, encoding="utf-8") as f:
                try:
                    payload = json.load(f)
                except json.JSONDecodeError:
                    continue  # download interrompido: será rebuscado

            if payload is None:
                continue  # dia sem dado, já consultado

            registro = parser(dia, payload)
            if registro is not None:
                registros[dia] = registro

        return registros

    @staticmethod
    def _inicio_padrao(existentes: set[date], ate: date, rebusca: int) -> date:
        if existentes:
            return max(existentes) - timedelta(days=rebusca - 1)
        return ate - timedelta(days=DIAS_PRIMEIRA_SINCRONIZACAO)

    # ------------------------------------------------------------------
    # Autenticação — o outro ponto de ajuste quando a biblioteca mudar
    # ------------------------------------------------------------------

    def _obter_cliente(self):
        """
        Cliente autenticado, retomando a sessão salva sempre que possível.

        O login completo pode pedir MFA, o que exige terminal. Numa execução
        agendada isso trava sem explicação, então aqui a falta de sessão válida
        vira erro explícito pedindo uma execução manual.
        """
        if self._cliente is not None:
            return self._cliente

        try:
            from garminconnect import Garmin
        except ImportError as erro:
            raise RuntimeError(
                "Biblioteca ausente. Instale com: pip install garminconnect"
            ) from erro

        tokens = Path(GARMIN_TOKENS)
        email = os.environ.get("GARMIN_EMAIL", "").strip()
        senha = os.environ.get("GARMIN_PASSWORD", "").strip()

        if not tokens.exists() and not (email and senha):
            raise RuntimeError(
                "Sem sessão salva e sem credenciais: preencha GARMIN_EMAIL e "
                "GARMIN_PASSWORD no .env (veja .env.example)."
            )

        def _pedir_mfa() -> str:
            """
            Só é chamado quando a sessão salva não serve mais.

            Numa execução agendada não há terminal: em vez de travar esperando
            uma digitação que nunca vem, falha explicando o que fazer.
            """
            if not sys.stdin.isatty():
                raise RuntimeError(
                    f"Sessão do Garmin expirada em {tokens} e o login pede MFA.\n"
                    "Rode 'python scripts/garmin_sync.py' manualmente uma vez para renovar."
                )
            return input("Código MFA do Garmin: ").strip()

        # `login(tokenstore)` faz as duas pontas: carrega a sessão salva se ela
        # ainda valer e, quando precisa logar de novo, grava a nova no mesmo
        # caminho. As credenciais do construtor só entram nesse segundo caso.
        tokens.parent.mkdir(parents=True, exist_ok=True)
        cliente = Garmin(email or None, senha or None, prompt_mfa=_pedir_mfa)
        cliente.login(str(tokens))

        self._cliente = cliente
        return cliente


def _intervalo(inicio: date, fim: date) -> list[date]:
    """Todos os dias de `inicio` a `fim`, inclusive. Vazio se a ordem inverter."""
    if inicio > fim:
        return []
    return [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]


# ===========================================================================
# Atividades
# ===========================================================================
#
# ATENÇÃO ÀS UNIDADES. O export GDPR guarda distância em centímetros, durações
# em milissegundos, velocidade a 1/10 do m/s e calorias a 10x — ver
# garmin.py::_parse_connect_json_record. **A API do Connect não faz nada disso**:
# devolve metros, segundos, m/s e calorias reais. Reaproveitar o parser do
# export aqui deixaria toda corrida 100x mais longa.
#
# Isso não é fé: `scripts/verificar_unidades_garmin.py` confere as atividades
# baixadas contra as mesmas atividades no Strava e acusa qualquer fator de
# escala. É a mesma técnica que validou as unidades do export.

ENDPOINTS_ATIVIDADE = {
    "activities": "get_activities_by_date",        # (inicio, fim) -> lista
    "hr_zones": "get_activity_hr_in_timezones",    # (activity_id) -> lista de zonas
    "power_zones": "get_activity_power_in_timezones",
    "exercise_sets": "get_activity_exercise_sets",
}

# Atividades podem ser renomeadas, ter o esporte corrigido ou subir com atraso,
# então a janela de rebusca é maior que a do wellness.
DIAS_REBUSCA_ATIVIDADES = 7

# O Garmin nomeia esporte em snake_case ("running"); o Strava usa CamelCase
# ("Run"). Como o Strava foi a base histórica, é o vocabulário dele que está nos
# CSVs e nos filtros dos notebooks (`sport == "Run"`). Uma atividade que venha
# só da API precisa chegar com o mesmo nome, senão some do filtro em silêncio.
ESPORTE_GARMIN_PARA_STRAVA = {
    "running": "Run",
    "trail_running": "Run",
    "treadmill_running": "Run",
    "indoor_running": "Run",
    "track_running": "Run",
    "virtual_run": "Run",
    "cycling": "Ride",
    "road_biking": "Ride",
    "indoor_cycling": "Ride",
    "mountain_biking": "Ride",
    "virtual_ride": "Ride",
    "gravel_cycling": "Ride",
    "lap_swimming": "Swim",
    "open_water_swimming": "Swim",
    "swimming": "Swim",
    "strength_training": "WeightTraining",
    "walking": "Walk",
    "casual_walking": "Walk",
    "speed_walking": "Walk",
    "hiking": "Hike",
    "fitness_equipment": "Workout",
    "indoor_cardio": "Workout",
    "other": "Workout",
}


def _normalizar_esporte(type_key: str | None) -> str:
    if not type_key:
        return "Workout"
    chave = str(type_key).lower()
    if chave in ESPORTE_GARMIN_PARA_STRAVA:
        return ESPORTE_GARMIN_PARA_STRAVA[chave]
    # Desconhecido: vira CamelCase para pelo menos não colidir com o snake_case
    return "".join(p.capitalize() for p in chave.split("_")) or "Workout"


def _zonas_para_segundos(payload, prefixo_chave="secsInZone") -> dict:
    """
    Converte a lista de zonas da API no dict `{"zone_1": segundos}` que
    build_tables espera.

    A API devolve uma lista com uma entrada por zona; o export GDPR trazia
    `hrTimeInZone_N` em milissegundos. Aqui já vem em segundos.
    """
    if not isinstance(payload, list):
        return {}

    zonas = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        numero = item.get("zoneNumber")
        segundos = item.get(prefixo_chave, item.get("secsInZone"))
        if numero is None or segundos is None:
            continue
        zonas[f"zone_{int(numero)}"] = float(segundos)
    return zonas


def _agrupar_series(payload) -> list[dict]:
    """
    Agrupa as séries de musculação por exercício.

    A API lista **cada série** separadamente; o export GDPR já vinha agregado
    por exercício em `summarizedExerciseSets`. Como `build_tables` espera o
    formato agregado, a soma é feita aqui: séries, repetições, volume
    (peso x repetições) e a carga máxima usada no exercício.
    """
    if isinstance(payload, dict):
        payload = payload.get("exerciseSets") or []
    if not isinstance(payload, list):
        return []

    por_exercicio: dict[str, dict] = {}
    for serie in payload:
        if not isinstance(serie, dict) or serie.get("setType") == "REST":
            continue

        exercicios = serie.get("exercises") or []
        categoria = None
        if exercicios and isinstance(exercicios[0], dict):
            categoria = exercicios[0].get("category") or exercicios[0].get("name")
        categoria = categoria or "UNKNOWN"

        reps = _inteiro(serie.get("repetitionCount")) or 0
        peso = _decimal(serie.get("weight")) or 0.0  # gramas na API
        peso_kg = peso / 1000 if peso > 100 else peso  # tolera as duas escalas
        duracao = _decimal(serie.get("duration")) or 0.0

        item = por_exercicio.setdefault(
            categoria,
            {"category": categoria, "sets": 0, "reps": 0, "volume": 0.0,
             "max_weight": None, "duration_s": 0.0},
        )
        item["sets"] += 1
        item["reps"] += reps
        item["volume"] += peso_kg * reps
        item["duration_s"] += duracao
        if peso_kg and (item["max_weight"] is None or peso_kg > item["max_weight"]):
            item["max_weight"] = peso_kg

    for item in por_exercicio.values():
        item["volume"] = round(item["volume"], 1)

    return list(por_exercicio.values())


def _parse_atividade(payload: dict, zonas_fc=None, zonas_pot=None, series=None):
    """
    Converte uma atividade da API em `Activity` — a mesma dataclass do export.

    Unidades da API (diferentes do export GDPR, ver o bloco acima): `distance`
    em metros, `duration` em segundos, `calories` em kcal reais, velocidades em
    m/s. Nenhuma divisão de escala aqui é acidente de digitação: não há nenhuma.

    O pace é derivado de distância/duração em vez de lido de `averageSpeed`,
    igual ao parser do export — bate com o que o relógio mostra e não depende de
    qual campo de velocidade veio preenchido.
    """
    from ingestion.garmin import Activity

    inicio = _buscar(payload, ("startTimeGMT",), ("startTimeLocal",))
    if not inicio:
        return None
    try:
        momento = datetime.fromisoformat(str(inicio).replace("Z", "").strip())
    except ValueError:
        return None

    duracao = _decimal(_buscar(payload, ("duration",))) or 0.0
    distancia = _decimal(_buscar(payload, ("distance",))) or 0.0

    pace = duracao / (distancia / 1000) if distancia > 0 and duracao > 0 else None

    extra = {
        "activity_type": _buscar(payload, ("activityType", "typeKey")),
        "location_name": _buscar(payload, ("locationName",)),
        "lap_count": _inteiro(_buscar(payload, ("lapCount",))),
        "moving_duration_s": _decimal(_buscar(payload, ("movingDuration",))),
        "elapsed_duration_s": _decimal(_buscar(payload, ("elapsedDuration",))),

        "elevation_loss_m": _decimal(_buscar(payload, ("elevationLoss",))),
        "min_elevation_m": _decimal(_buscar(payload, ("minElevation",))),
        "max_elevation_m": _decimal(_buscar(payload, ("maxElevation",))),

        "avg_speed_mps": _decimal(_buscar(payload, ("averageSpeed",))),
        "max_speed_mps": _decimal(_buscar(payload, ("maxSpeed",))),

        # avgRunningCadence conta uma perna só; o campo "double" é o passos/min real
        "avg_cadence_spm": _decimal(
            _buscar(payload, ("averageRunningCadenceInStepsPerMinute",))
        ),
        "max_cadence_spm": _decimal(
            _buscar(payload, ("maxRunningCadenceInStepsPerMinute",))
        ),
        "stride_length_cm": _decimal(_buscar(payload, ("avgStrideLength",))),
        "vertical_oscillation_cm": _decimal(_buscar(payload, ("avgVerticalOscillation",))),
        "ground_contact_time_ms": _decimal(_buscar(payload, ("avgGroundContactTime",))),
        "vertical_ratio_pct": _decimal(_buscar(payload, ("avgVerticalRatio",))),

        "norm_power_w": _decimal(_buscar(payload, ("normPower",))),
        "max_power_w": _decimal(_buscar(payload, ("maxPower",))),

        "vo2max": _decimal(_buscar(payload, ("vO2MaxValue",))),
        "aerobic_te": _decimal(_buscar(payload, ("aerobicTrainingEffect",))),
        "anaerobic_te": _decimal(_buscar(payload, ("anaerobicTrainingEffect",))),
        "training_effect_label": _buscar(payload, ("trainingEffectLabel",)),
        "aerobic_te_message": _buscar(payload, ("aerobicTrainingEffectMessage",)),
        "anaerobic_te_message": _buscar(payload, ("anaerobicTrainingEffectMessage",)),
        "bmr_calories": _decimal(_buscar(payload, ("bmrCalories",))),
        "body_battery_delta": _decimal(_buscar(payload, ("differenceBodyBattery",))),

        # Na API o RPE já vem em 0-100 como no export: volta para a escala 1-10
        "workout_feel": _buscar(payload, ("workoutFeel",)),
        "workout_rpe": (
            _decimal(_buscar(payload, ("workoutRpe",))) / 10
            if _buscar(payload, ("workoutRpe",)) is not None
            else None
        ),

        "steps": _inteiro(_buscar(payload, ("steps",))),
        "moderate_intensity_minutes": _inteiro(_buscar(payload, ("moderateIntensityMinutes",))),
        "vigorous_intensity_minutes": _inteiro(_buscar(payload, ("vigorousIntensityMinutes",))),
        "min_temperature_c": _decimal(_buscar(payload, ("minTemperature",))),
        "max_temperature_c": _decimal(_buscar(payload, ("maxTemperature",))),

        "total_sets": _inteiro(_buscar(payload, ("totalSets",))),
        "active_sets": _inteiro(_buscar(payload, ("activeSets",))),
        "total_reps": _inteiro(_buscar(payload, ("totalReps",))),

        "strokes": _inteiro(_buscar(payload, ("strokes",))),
        "avg_swolf": _decimal(_buscar(payload, ("avgSwolf",))),
        "pool_length": _decimal(_buscar(payload, ("poolLength",))),

        "hr_zones_seconds": zonas_fc or {},
        "power_zones_seconds": zonas_pot or {},
        "exercise_sets": series or [],
    }
    extra = {k: v for k, v in extra.items() if v not in (None, {}, [])}

    return Activity(
        source="garmin",
        activity_id=str(_buscar(payload, ("activityId",)) or ""),
        name=_buscar(payload, ("activityName",)) or "Atividade Garmin",
        sport=_normalizar_esporte(_buscar(payload, ("activityType", "typeKey"))),
        start_time=momento,
        duration_seconds=duracao,
        distance_meters=distancia,
        avg_heart_rate=_decimal(_buscar(payload, ("averageHR",))),
        max_heart_rate=_decimal(_buscar(payload, ("maxHR",))),
        avg_pace_sec_per_km=pace,
        elevation_gain_meters=_decimal(_buscar(payload, ("elevationGain",))),
        avg_power_watts=_decimal(_buscar(payload, ("avgPower",))),
        calories=_decimal(_buscar(payload, ("calories",))),
        extra=extra,
    )
