"""
Catálogo de atividades: o índice do que já foi baixado do Garmin.

Uma linha por `activityId`, num CSV em `dados/catalogo_atividades.csv`. Não é
tabela de análise — é o que a sincronização consulta para decidir o que buscar:

- **id novo** → baixa resumo e todos os detalhes
- **id conhecido com resumo diferente** (renomeou, trocou esporte, editou RPE)
  → regrava o resumo; os detalhes (zonas, séries, FIT) não mudam depois do treino
- **id conhecido e igual** → nada
- **id que sumiu da lista** dentro da janela consultada → marca `removida_em`,
  sem apagar o arquivo cru. Apagar mudaria o histórico em silêncio; marcar deixa
  a decisão para quem for analisar.

"Diferente" é decidido por hash do resumo, calculado só sobre os campos que
importam para análise (`CAMPOS_HASH`). Hash do JSON inteiro acusaria mudança a
cada sync, porque o Garmin devolve contadores sociais e URLs de imagem que
mudam sozinhos.

O catálogo também guarda a classificação do typeKey (categoria e ambiente) e a
versão das regras que a produziu — ver `ingestion.classificacao`.

Gravação atômica: escreve num `.tmp` e troca pelo `os.replace`. Um sync
interrompido no meio da escrita não deixa o catálogo pela metade.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path

from ingestion.classificacao import VERSAO_REGRAS, classificar_type_key

# Campos do resumo que, se mudarem, justificam regravar. Ficam de fora os que
# mudam sem o treino mudar (kudos, fotos, privacidade, URLs de perfil).
CAMPOS_HASH = (
    "activityName",
    "activityType",
    "startTimeGMT",
    "duration",
    "distance",
    "averageHR",
    "maxHR",
    "calories",
    "workoutRpe",
    "workoutFeel",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "totalSets",
    "totalReps",
)


@dataclass
class LinhaCatalogo:
    activity_id: str
    start_time_utc: str
    type_key: str
    categoria: str
    ambiente: str
    fonte_resumo: str  # personal | garminconnect
    hash_resumo: str
    tem_fit: bool = False
    tem_series: bool = False
    baixado_em: str = ""
    atualizado_em: str = ""
    removida_em: str = ""
    versao_regras: int = VERSAO_REGRAS


COLUNAS = [f.name for f in fields(LinhaCatalogo)]


def hash_resumo(payload: dict) -> str:
    """Hash estável dos campos relevantes do resumo (ordem de chave fixa)."""
    recorte = {k: payload.get(k) for k in CAMPOS_HASH}
    texto = json.dumps(recorte, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()[:16]


def _type_key(payload: dict) -> str:
    tipo = payload.get("activityType")
    if isinstance(tipo, dict):
        return str(tipo.get("typeKey") or "")
    return str(tipo or "")


def _inicio_utc(payload: dict) -> str:
    return str(payload.get("startTimeGMT") or payload.get("startTimeLocal") or "").strip()


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Catalogo:
    """
    Catálogo em memória, lido e gravado de um CSV.

        cat = Catalogo.abrir(caminho)
        status = cat.registrar(payload, fonte="personal")   # "nova" | "alterada" | "igual"
        cat.salvar()
    """

    def __init__(self, caminho: str | Path, linhas: dict[str, LinhaCatalogo] | None = None):
        self.caminho = Path(caminho)
        self.linhas: dict[str, LinhaCatalogo] = linhas or {}

    # ------------------------------------------------------------------
    # Leitura e escrita
    # ------------------------------------------------------------------

    @classmethod
    def abrir(cls, caminho: str | Path) -> Catalogo:
        caminho = Path(caminho)
        linhas: dict[str, LinhaCatalogo] = {}
        if caminho.exists():
            with open(caminho, encoding="utf-8-sig", newline="") as f:
                for bruto in csv.DictReader(f):
                    linha = cls._de_csv(bruto)
                    linhas[linha.activity_id] = linha
        return cls(caminho, linhas)

    @staticmethod
    def _de_csv(bruto: dict) -> LinhaCatalogo:
        # CSV só tem texto: devolve os tipos certos para bool e int
        def booleano(v):
            return str(v).strip().lower() in ("true", "1", "sim")

        return LinhaCatalogo(
            activity_id=bruto["activity_id"],
            start_time_utc=bruto.get("start_time_utc", ""),
            type_key=bruto.get("type_key", ""),
            categoria=bruto.get("categoria", ""),
            ambiente=bruto.get("ambiente", ""),
            fonte_resumo=bruto.get("fonte_resumo", ""),
            hash_resumo=bruto.get("hash_resumo", ""),
            tem_fit=booleano(bruto.get("tem_fit")),
            tem_series=booleano(bruto.get("tem_series")),
            baixado_em=bruto.get("baixado_em", ""),
            atualizado_em=bruto.get("atualizado_em", ""),
            removida_em=bruto.get("removida_em", ""),
            versao_regras=int(bruto.get("versao_regras") or 0),
        )

    def salvar(self) -> Path:
        """Grava ordenado por data e id, de forma atômica (tmp + replace)."""
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_suffix(self.caminho.suffix + ".tmp")
        ordenadas = sorted(self.linhas.values(), key=lambda lin: (lin.start_time_utc, lin.activity_id))
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUNAS)
            writer.writeheader()
            for linha in ordenadas:
                writer.writerow(asdict(linha))
        os.replace(tmp, self.caminho)
        return self.caminho

    # ------------------------------------------------------------------
    # Detecção
    # ------------------------------------------------------------------

    def registrar(self, payload: dict, fonte: str, agora: str | None = None) -> str:
        """
        Registra um resumo vindo da lista de atividades e diz o que mudou.

        Devolve "nova", "alterada" ou "igual". Uma atividade que tinha sido
        marcada como removida e reaparece volta a valer (e conta como alterada).
        """
        aid = str(payload.get("activityId") or "").strip()
        if not aid:
            raise ValueError("resumo sem activityId")

        agora = agora or _agora()
        novo_hash = hash_resumo(payload)
        type_key = _type_key(payload)
        categoria, ambiente = classificar_type_key(type_key)

        atual = self.linhas.get(aid)
        if atual is None:
            self.linhas[aid] = LinhaCatalogo(
                activity_id=aid,
                start_time_utc=_inicio_utc(payload),
                type_key=type_key,
                categoria=categoria,
                ambiente=ambiente or "",
                fonte_resumo=fonte,
                hash_resumo=novo_hash,
                baixado_em=agora,
                atualizado_em=agora,
            )
            return "nova"

        reapareceu = bool(atual.removida_em)
        mudou = atual.hash_resumo != novo_hash
        # Regra nova de classificação também atualiza a linha, mesmo sem o
        # resumo ter mudado
        regra_velha = atual.versao_regras != VERSAO_REGRAS

        if not (mudou or reapareceu or regra_velha):
            return "igual"

        atual.start_time_utc = _inicio_utc(payload) or atual.start_time_utc
        atual.type_key = type_key
        atual.categoria = categoria
        atual.ambiente = ambiente or ""
        atual.fonte_resumo = fonte
        atual.hash_resumo = novo_hash
        atual.removida_em = ""
        atual.versao_regras = VERSAO_REGRAS
        atual.atualizado_em = agora
        return "alterada" if (mudou or reapareceu) else "igual"

    def marcar_detalhes(self, activity_id: str, *, tem_fit: bool | None = None,
                        tem_series: bool | None = None) -> None:
        """Anota quais blocos de detalhe já estão em disco para esta atividade."""
        linha = self.linhas.get(str(activity_id))
        if linha is None:
            return
        if tem_fit is not None:
            linha.tem_fit = tem_fit
        if tem_series is not None:
            linha.tem_series = tem_series

    def marcar_removidas(self, ids_vistos: set[str], desde: date, ate: date,
                         agora: str | None = None) -> list[str]:
        """
        Marca como removidas as atividades da janela [desde, ate] que não
        vieram na lista desta vez. Só olha dentro da janela consultada: fora
        dela, não ter vindo não quer dizer nada.
        """
        agora = agora or _agora()
        removidas = []
        for aid, linha in self.linhas.items():
            if linha.removida_em or aid in ids_vistos:
                continue
            dia = _dia(linha.start_time_utc)
            if dia is not None and desde <= dia <= ate:
                linha.removida_em = agora
                removidas.append(aid)
        return removidas

    def marca_dagua(self, recuo_dias: int) -> date | None:
        """
        Data a partir da qual buscar: última atividade conhecida menos o recuo.
        None quando o catálogo está vazio (primeiro sync).
        """
        dias = [_dia(lin.start_time_utc) for lin in self.linhas.values() if not lin.removida_em]
        dias = [d for d in dias if d is not None]
        if not dias:
            return None
        return max(dias) - timedelta(days=recuo_dias)

    def ativas(self) -> set[str]:
        """Ids não removidos — o filtro que o loader aplica antes de montar as atividades."""
        return {aid for aid, lin in self.linhas.items() if not lin.removida_em}

    def removidas(self) -> set[str]:
        return {aid for aid, lin in self.linhas.items() if lin.removida_em}


def _dia(texto: str) -> date | None:
    if not texto:
        return None
    try:
        return datetime.fromisoformat(str(texto).replace("Z", "").strip()).date()
    except ValueError:
        return None
