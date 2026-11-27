#!/usr/bin/env python
"""
Teste exploratório da Personal API: o que ela entrega de fato, campo a campo.

Roda UMA vez (ou sempre que a Garmin mexer na API) antes de confiar no sync.
Não grava nada no cache do pipeline: tudo vai para
`dados/exploracao_personal/<data-hora>/`, com as respostas cruas e um
`relatorio.md` que responde três perguntas:

1. Quais rotas funcionam para a sua conta? (a coluna "rota" do relatório é o
   que deve ficar sozinha em `ROTAS`, no garmin_personal.py)
2. O payload é igual ao do garminconnect? Compara com o que já está em
   `dados/garmin_api/` para as mesmas atividades e os mesmos dias. Se for, os
   parsers atuais servem sem mudança.
3. O FIT vem completo? Lista os tipos de mensagem (record a 1 Hz, set da
   musculação...) — precisa do fitparse, que já é dependência do projeto.

Uso:
    python scripts/explorar_personal_api.py                    # escolhe sozinho
    python scripts/explorar_personal_api.py --atividades 24804034738 24749705178
    python scripts/explorar_personal_api.py --dias 7 --historico 2024-06-01

Requer GARMIN_PERSONAL_API_KEY no .env.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from config import DADOS_DIR, ENV_FILE, GARMIN_API_DIR
from ingestion.garmin_personal import (
    ApiError,
    ClientePersonal,
    IndisponivelError,
    escopos_do_jwt,
)

load_dotenv(ENV_FILE)

# Campos do resumo cujo VALOR é comparado (não só a presença), com tolerância
# relativa. São os que alimentam activities.csv e o TSS.
CAMPOS_VALOR = ("distance", "duration", "averageHR", "maxHR", "calories", "avgPower",
                "aerobicTrainingEffect", "workoutRpe", "elevationGain", "totalSets")
TOLERANCIA = 0.01


# ---------------------------------------------------------------------------
# Comparação
# ---------------------------------------------------------------------------

def _chaves(payload) -> set[str]:
    if isinstance(payload, list):
        payload = payload[0] if payload and isinstance(payload[0], dict) else {}
    return set(payload) if isinstance(payload, dict) else set()


def comparar(antigo, novo) -> dict:
    """Resumo da diferença entre a resposta do garminconnect e a da Personal."""
    if antigo is None:
        return {"situacao": "sem referência no cache"}
    if novo is None:
        return {"situacao": "Personal devolveu vazio"}

    ka, kn = _chaves(antigo), _chaves(novo)
    resultado = {
        "situacao": "mesmas chaves" if ka == kn else "chaves diferentes",
        "so_no_garminconnect": sorted(ka - kn)[:15],
        "so_na_personal": sorted(kn - ka)[:15],
    }

    if isinstance(antigo, dict) and isinstance(novo, dict):
        divergentes = []
        for campo in CAMPOS_VALOR:
            a, n = antigo.get(campo), novo.get(campo)
            if a is None and n is None:
                continue
            try:
                igual = abs(float(a) - float(n)) <= TOLERANCIA * max(abs(float(a)), 1.0)
            except (TypeError, ValueError):
                igual = a == n
            if not igual:
                divergentes.append(f"{campo}: {a} vs {n}")
        resultado["valores_divergentes"] = divergentes
    elif isinstance(antigo, list) and isinstance(novo, list):
        resultado["itens"] = f"{len(antigo)} vs {len(novo)}"
    return resultado


def inspecionar_fit(conteudo: bytes) -> dict:
    """Tipos de mensagem do FIT e campos do `record` — diz se vem completo."""
    if not conteudo:
        return {"situacao": "vazio"}
    bruto = conteudo
    if zipfile.is_zipfile(io.BytesIO(conteudo)):
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            nomes = [n for n in z.namelist() if n.lower().endswith(".fit")]
            if not nomes:
                return {"situacao": f"zip sem .fit: {z.namelist()}"}
            bruto = z.read(nomes[0])

    info: dict = {"bytes": len(bruto)}
    try:
        import fitparse
    except ImportError:
        info["situacao"] = "fitparse não instalado: só o tamanho foi medido"
        return info

    contagem: dict[str, int] = {}
    campos_record: set[str] = set()
    campos_set: set[str] = set()
    for msg in fitparse.FitFile(io.BytesIO(bruto)).get_messages():
        contagem[msg.name] = contagem.get(msg.name, 0) + 1
        if msg.name == "record" and len(campos_record) < 60:
            campos_record.update(f.name for f in msg.fields if f.value is not None)
        if msg.name == "set":
            campos_set.update(f.name for f in msg.fields if f.value is not None)

    info["mensagens"] = dict(sorted(contagem.items(), key=lambda kv: -kv[1])[:20])
    info["campos_record"] = sorted(campos_record)
    info["campos_set"] = sorted(campos_set)
    return info


# ---------------------------------------------------------------------------
# Escolha automática do que testar
# ---------------------------------------------------------------------------

def _ler(caminho: Path):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def escolher_atividades() -> list[str]:
    """Uma de cada: esteira, corrida de rua, musculação — as mais recentes do cache."""
    alvo = {"treadmill_running": None, "running": None, "strength_training": None}
    arquivos = sorted((GARMIN_API_DIR / "activities").glob("*.json"), reverse=True)
    for arquivo in arquivos:
        payload = _ler(arquivo) or {}
        tipo = (payload.get("activityType") or {}).get("typeKey")
        if tipo in alvo and alvo[tipo] is None:
            alvo[tipo] = arquivo.stem
    return [aid for aid in alvo.values() if aid]


def escolher_dias(n: int) -> list[str]:
    """Os n dias mais recentes que já têm sono no cache (para ter com o que comparar)."""
    dias = sorted((p.stem for p in (GARMIN_API_DIR / "sleep").glob("*.json")), reverse=True)
    return dias[:n] or [(date.today() - timedelta(days=i)).isoformat() for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def _detalhe(cmp: dict) -> str:
    """O que a comparação achou, numa linha de tabela (sem a situação, que tem coluna própria)."""
    return json.dumps({k: v for k, v in cmp.items() if v and k != "situacao"}, ensure_ascii=False)


def chamar(cliente, metodo, *args):
    """Chama e devolve (payload, erro) sem deixar uma falha derrubar o teste."""
    try:
        return getattr(cliente, metodo)(*args), None
    except (IndisponivelError, ApiError, RuntimeError) as erro:
        return None, str(erro)[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description="Teste exploratório da Personal API")
    parser.add_argument("--atividades", nargs="*", help="activityIds a testar")
    parser.add_argument("--dias", type=int, default=7, help="dias de wellness a testar")
    parser.add_argument("--historico", default="2024-06-01",
                        help="data antiga para testar até onde a API volta")
    args = parser.parse_args()

    saida = DADOS_DIR / "exploracao_personal" / datetime.now().strftime("%Y%m%d-%H%M%S")
    saida.mkdir(parents=True, exist_ok=True)

    # Token de teste fica só nesta pasta: não mistura com o do pipeline
    try:
        cliente = ClientePersonal.do_ambiente(cache_token=saida / "token.json", pausa=1.0)
        token = cliente.token()
    except RuntimeError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1

    linhas: list[tuple[str, str, str, str]] = []  # (item, alvo, resultado, detalhe)
    print(f"Token ok. Escopos: {', '.join(escopos_do_jwt(token)) or 'não declarados no JWT'}")
    linhas.append(("token", "-", "ok", ", ".join(escopos_do_jwt(token)) or "sem escopos no JWT"))

    def guardar(nome: str, payload) -> None:
        if isinstance(payload, (bytes, bytearray)):
            (saida / nome).write_bytes(payload)
        elif payload is not None:
            (saida / f"{nome}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )

    # 1. Perfil (de onde sai o displayName, se as rotas de wellness pedirem)
    perfil, erro = chamar(cliente, "get_social_profile")
    guardar("perfil", perfil)
    nome = (perfil or {}).get("displayName") if isinstance(perfil, dict) else None
    if nome and not cliente._display_name:
        cliente._display_name = nome  # deixa as rotas com {display_name} serem tentadas
    linhas.append(("perfil", "-", "ok" if perfil else "falhou", erro or f"displayName={nome}"))

    # 2. Atividades: resumo, detalhes, FIT, comparados com o cache
    ids = args.atividades or escolher_atividades()
    print(f"Atividades testadas: {ids}")
    for aid in ids:
        cache = _ler(GARMIN_API_DIR / "activities" / f"{aid}.json") or {}
        dia = str(cache.get("startTimeLocal") or "")[:10] or date.today().isoformat()

        lista, erro = chamar(cliente, "get_activities_by_date", dia, dia)
        resumo = next((a for a in lista or [] if str(a.get("activityId")) == aid), None)
        guardar(f"resumo_{aid}", resumo)
        cmp = comparar(cache or None, resumo)
        linhas.append(("resumo", aid, cmp["situacao"] if not erro else "falhou",
                       erro or _detalhe(cmp)))

        for metodo, pasta in (("get_activity_hr_in_timezones", "hr_zones"),
                              ("get_activity_power_in_timezones", "power_zones"),
                              ("get_activity_exercise_sets", "exercise_sets")):
            payload, erro = chamar(cliente, metodo, aid)
            guardar(f"{pasta}_{aid}", payload)
            cmp = comparar(_ler(GARMIN_API_DIR / pasta / f"{aid}.json"), payload)
            linhas.append((pasta, aid, "falhou" if erro else cmp["situacao"],
                           erro or _detalhe(cmp)))

        fit, erro = chamar(cliente, "download_activity_fit", aid)
        guardar(f"fit_{aid}.zip", fit)
        info = inspecionar_fit(fit) if fit else {}
        linhas.append(("fit", aid, "falhou" if erro else info.get("situacao", "ok"),
                       erro or json.dumps(info, ensure_ascii=False)))

    # 3. Wellness nos mesmos dias que já estão no cache
    for dia in escolher_dias(args.dias):
        for metodo, pasta in (("get_sleep_data", "sleep"), ("get_stats", "summary"),
                              ("get_hrv_data", "hrv"), ("get_spo2_data", "spo2"),
                              ("get_daily_heart_rate", None)):
            payload, erro = chamar(cliente, metodo, dia)
            guardar(f"{metodo}_{dia}", payload)
            referencia = _ler(GARMIN_API_DIR / pasta / f"{dia}.json") if pasta else None
            if pasta:
                cmp = comparar(referencia, payload)
            else:
                cmp = {"situacao": "ok" if payload else "vazio"}
            linhas.append((metodo, dia, "falhou" if erro else cmp["situacao"],
                           erro or _detalhe(cmp)))

    # 4. Até onde o histórico volta
    inicio = date.fromisoformat(args.historico)
    antigas, erro = chamar(cliente, "get_activities_by_date",
                           inicio.isoformat(), (inicio + timedelta(days=30)).isoformat())
    resultado = "falhou" if erro else f"{len(antigas or [])} atividades"
    linhas.append(("historico", args.historico, resultado,
                   erro or "zero pode ser só mês sem treino: confira no Connect"))

    # 5. Relatório
    rotas = sorted({(m, r, s) for m, r, s in cliente.log})
    relatorio = [
        f"# Exploração da Personal API — {datetime.now():%d/%m/%Y %H:%M}",
        "",
        "## Resultado por item",
        "",
        "| item | alvo | resultado | detalhe |",
        "|---|---|---|---|",
        *[f"| {i} | {a} | {r} | {d.replace('|', '/')} |" for i, a, r, d in linhas],
        "",
        "## Rotas tentadas (método, rota, HTTP)",
        "",
        "Rotas com 2xx são as que devem ficar em `ROTAS` (garmin_personal.py).",
        "",
        *[f"- `{m}` `{r}` → {s}" for m, r, s in rotas],
    ]
    (saida / "relatorio.md").write_text("\n".join(relatorio), encoding="utf-8")

    ok = sum(1 for linha in linhas if linha[2] not in ("falhou",))
    print(f"\n{ok}/{len(linhas)} itens responderam. Relatório: {saida / 'relatorio.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
