# Athlete Coach

Pipeline que extrai treinos e dados de saúde do Garmin e do Strava, normaliza
as unidades e escreve tudo em CSVs prontos para análise.

## Objetivo

O projeto está em transição. Nasceu para gerar um arquivo Markdown de contexto
para um Project no claude.ai, e hoje o foco é o dado em si.

| Fase | O que é | Status |
|---|---|---|
| 1. Extração | Ler os exports, corrigir unidades, gerar CSVs normalizados | **pronto** |
| 2. Análise | Explorar os dados: volume, carga, sono, tendências | **em andamento** |
| 3. Relatório semanal | App que sincroniza toda semana e compara com as anteriores | planejado |
| 4. Modelo preditivo | Encontrar padrões e tendências (fadiga, risco, progressão) | ideia |

A saída Markdown (`athlete_context.md`) continua sendo gerada e funciona, mas
deixou de ser o produto principal.

## Setup

```bash
# 1. Criar e ativar o venv (fica na RAIZ do repositório, um nível acima daqui)
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/Mac

# 2. Instalar o projeto em modo editável (obrigatório)
#    O pyproject.toml está em athlete-coach/, NÃO na raiz do repositório.
pip install -e "./athlete-coach[dev,notebooks]"   # rodando da raiz
pip install -e ".[dev,notebooks]"                 # rodando de dentro de athlete-coach/

# 3. Credenciais do Strava
cp .env.example .env          # preencha CLIENT_ID e CLIENT_SECRET
#    Crie o app em https://www.strava.com/settings/api
#    (Authorization Callback Domain: localhost)

# 4. Só se for versionar: registrar o filtro que limpa as saídas dos notebooks
nbstripout --install
```

O passo 4 é obrigatório em qualquer clone que vá commitar. O `.gitattributes`
declara o filtro, mas quem o executa é o driver registrado no `.git/config`
local — sem `nbstripout --install`, o git ignora a declaração **em silêncio** e
commita os notebooks com as saídas dentro. Confira com
`git config --get filter.nbstripout.clean`.

O `pip install -e .` não é opcional: o projeto usa layout `src/` e sem a
instalação nenhum import funciona (`ModuleNotFoundError: config`). Ao recriar
o venv, rode de novo.

## Uso

```bash
python scripts/strava_export.py    # sincroniza atividades novas do Strava
python scripts/export_context.py   # gera os CSVs + athlete_context.md
```

O primeiro comando abre o navegador para autorizar via OAuth na primeira vez e
guarda o refresh token no `.env`. O segundo lê os dois exports e escreve tudo
em `dados/processed/`.

Os dados do Garmin vêm de um export GDPR manual (garmin.com → Menu do usuário →
Relatórios e privacidade → Exportar dados), que precisa ser descompactado em
`dados/garmin/`. Como é manual, a atualização é mensal; o Strava é semanal.

Para explorar em notebook, comece por `notebooks/00_overview.ipynb` — ele mapeia o que
existe nos CSVs; `01_wellness`, `02_activities` e `03_load_recovery` analisam cada recorte.

### Sincronização semanal

```bash
python scripts/atualizar.py        # sync do Garmin + reprocessa os CSVs (é o que o agendador chama)
```

O `garmin_sync.py` (chamado pelo `atualizar.py`) tem duas fontes:

- **Personal API** (oficial, liberada em out/2026): ativa quando
  `GARMIN_PERSONAL_API_KEY` está no `.env`. Gere a chave em Garmin Connect web →
  Configurações → Personal API Key. Não pede MFA, então o agendador roda sozinho
  até a chave expirar (30 a 365 dias, você escolhe).
- **`garminconnect`** (não-oficial): usada quando não há chave, e como fallback
  só para o que a Personal API não entrega (VO2max). O login pode pedir MFA, então
  a primeira execução dessa fonte tem que ser manual.

**Antes de confiar na Personal API, rode o teste exploratório** uma vez:

```bash
python scripts/explorar_personal_api.py
```

Ele compara campo a campo o que a API oficial devolve com o que o
`garminconnect` já gravou em `dados/garmin_api/` (mesmas atividades, mesmos
dias), inspeciona o FIT e grava um relatório em `dados/exploracao_personal/`.
As rotas de zonas, séries, sono e SpO2 em `ingestion/garmin_personal.py` são
candidatas até esse teste confirmar.

Como o incremental funciona:

- **Atividades**: `dados/catalogo_atividades.csv` guarda uma linha por
  `activityId`. A busca começa na última atividade conhecida menos 7 dias; cada
  resumo é comparado por hash e sai como nova, alterada ou igual. Zonas, séries
  e o FIT (`dados/garmin_api/fit/<id>.zip`) são baixados uma vez só. Atividade
  apagada no Connect é marcada como removida no catálogo e sai da análise, mas o
  arquivo cru fica.
- **Wellness**: só os dias que faltam, mais uma rebusca dos últimos 3 dias
  (o Garmin revisa sono e HRV depois do fato).
- **Tabelas**: `dados/processed/` é reconstruído inteiro a cada execução, a
  partir do cache cru. São segundos de CPU, e é o que garante que rodar duas
  vezes dá o mesmo resultado (há teste para isso).

O Strava (`strava_export.py`) virou só o histórico anterior ao relógio.

## Os dados

Sete CSVs em `dados/processed/`, sobrescritos a cada execução. Todos em UTF-8
com BOM (o Excel abre os acentos corretamente).

### `activities.csv` — 176 linhas, 83 colunas

A tabela principal: uma linha por treino, unindo Garmin e Strava.

- **Identificação:** `date`, `time`, `source`, `activity_id`, `name`, `sport`
- **Básico:** `duration_minutes`, `distance_km`, `avg_pace`, `avg_heart_rate`,
  `max_heart_rate`, `elevation_gain_meters`, `calories`, `tss`
- **Dinâmica de corrida:** `avg_cadence_spm`, `stride_length_cm`,
  `ground_contact_time_ms`, `vertical_oscillation_cm`, `vertical_ratio_pct`
- **Potência:** `avg_power_watts`, `norm_power_w`, `max_power_w`,
  `weighted_avg_power_w`, `kilojoules`
- **Fisiologia:** `vo2max`, `aerobic_te`, `anaerobic_te`, `body_battery_delta`,
  `bmr_calories`
- **Percepção subjetiva:** `workout_feel` (0-100), `workout_rpe` (1-10)
- **Contexto:** `location_name`, `min_temperature_c`, `max_temperature_c`,
  `device_name`, `gear_id`, `start_lat`, `start_lng`
- **Social/Strava:** `suffer_score`, `kudos_count`, `pr_count`,
  `achievement_count`, `is_commute`, `is_trainer`, `is_manual`

### `wellness_daily.csv` — 119 linhas, 25 colunas

Um dia por linha. Só existe no Garmin — o Strava não tem nada equivalente.

`sleep_hours`, `sleep_deep_pct`, `sleep_rem_pct`, `sleep_score`,
`sleep_feedback`, `avg_respiration`, `resting_hr`, `hrv`, `spo2`,
`avg_stress`, `max_stress`, `steps`, `active_calories`, `vo2max`

### `training_load_daily.csv` — 366 linhas

Série diária contínua (inclui dias sem treino): `date`, `tss`, `ctl`, `atl`, `tsb`.

CTL é a média móvel exponencial do TSS em 42 dias (condicionamento), ATL em
7 dias (fadiga), TSB é a diferença (forma).

### `weekly_summary.csv` — 52 linhas

Agregado por semana ISO: `week_start`, `total_activities`, `total_distance_km`,
`total_duration_minutes`, `avg_heart_rate`, `longest_activity_km`, `avg_pace`,
`sports`.

### `activity_hr_zones.csv` / `activity_power_zones.csv` — formato longo

Uma linha por atividade **por zona**, com `seconds`, `minutes` e
`pct_of_activity`. Formato longo para facilitar agrupar (ex.: somar o tempo em
zona 4 do mês).

### `strength_sets.csv` — 111 linhas

Uma linha por exercício por sessão: `exercise`, `sets`, `reps`, `volume`,
`max_weight`, `duration_s`.

## Antes de analisar: o que você precisa saber

Estas são as armadilhas reais dos dados, verificadas empiricamente.

| Ponto de atenção | Impacto |
|---|---|
| **Wellness cobre só 08/04/2026 a 04/08/2026** (119 dias), mas as atividades vão de 06/2024 a 08/2026 | Apenas **81 das 176 atividades** têm sono/HRV/estresse no mesmo dia. Cruzar treino × recuperação só é possível nessa janela |
| **`calories` do Garmin é dividido por 10** — inferido, não confirmado | Os valores brutos davam 39-51 kcal/min (impossível); ÷10 dá 2-12 kcal/min. A escala é plausível mas não validada contra fonte externa. **Não use como número absoluto** |
| **`calories` é vazio em treinos só-Strava** | O export do Strava não tem esse campo. 94 das 176 linhas ficam sem calorias |
| **`max_weight` e `volume` são sempre 0** em `strength_sets.csv` | O relógio conta séries e repetições, mas não registra carga. Progressão de força precisa de outra fonte |
| **`tss` usa referências fixas** (FTP 250 W só para bike, FC 185 para o resto) | Se não forem os seus valores, a escala inteira de CTL/ATL/TSB está deslocada. Ajuste `FTP_BIKE_W` e `FC_REFERENCIA` em `processing/metrics.py`. Até nov/2026 a potência de corrida era dividida pelo FTP de bike, o que inflava o TSS (344 numa esteira de 47 min) |
| **`categoria`, `ambiente` e `flags_qualidade`** em `activities.csv` | Classificação por regras (`ingestion/classificacao.py`): separa esteira de rua e marca problemas como `potencia_esteira`, `sem_fc`, `series_sem_carga`. Filtre esteira por `ambiente`, não por `sport` |
| **`source` distingue a origem** | `strava` (94) = só Strava; `strava+garmin` (82) = os dois fundidos. Colunas de zona, RPE e dinâmica só existem nas linhas fundidas |
| **`vo2max` preenchido em 36 de 176 linhas** | O Garmin só estima em corridas ao ar livre com GPS |
| **Distribuição desbalanceada** | Run 126, WeightTraining 39, Workout 5, Walk 3, Ride 2, Swim 1. Só corrida tem massa para análise estatística |

### Convenções de unidade do export Garmin

O export GDPR não documenta as unidades. Estas foram descobertas e conferidas:

- `distance` e todos os campos de elevação: **centímetros**
- `duration`, `movingDuration`, `hrTimeInZone_*`, `powerTimeInZone_*`: **milissegundos**
- `avgSpeed`, `maxSpeed`: **1/10** do m/s real
- `calories`, `bmrCalories`: **10x** o valor real
- `workoutRpe`: escala 0-100 (dividido por 10 → RPE 1-10)
- `avgRunCadence` conta uma perna; `avgDoubleCadence` é o passos/min real

Tudo isso já é convertido na ingestão — os CSVs estão em unidades reais.

## Fluxo de dados

```
dados/garmin/.../summarizedActivities.json (82 atividades)
       │  GarminIngestion.load_garmin_connect_json()
       │  cm→m, ms→s, calorias÷10
       ▼
dados/strava/activities.json (176 atividades)
       │  StravaIngestion.load_export_json()
       ▼
fundir_garmin_no_strava()     ── funde o treino Garmin no gêmeo do Strava
       │                         (1:1, ±10 min, ±200 m, mesma categoria)
       │                         mantém zonas de FC, RPE, séries, calorias
       ▼
lista unificada de Activity
       │
       ├──→ TrainingLoadMetrics  ── CTL/ATL/TSB diário + resumos semanais
       ├──→ build_tables()       ── dados/processed/*.csv
       └──→ seções markdown      ── athlete_context.md

dados/garmin/.../DI-Connect-Wellness/*.json    ┐
dados/garmin/.../DI-Connect-Aggregator/UDS*    ├─→ GarminWellnessIngestion.load_all()
dados/garmin/.../DI-Connect-Metrics/Max*       ┘         │
                                                          ▼
                                            dict[date, DailyWellness] → wellness_daily.csv
```

O relógio Garmin sincroniza sozinho com o Strava, então as mesmas sessões
aparecem nos dois exports. Contá-las duas vezes dobraria o CTL/ATL, mas
descartar o lado Garmin perderia tudo que o Strava não recebe — por isso os
registros são fundidos, e não deduplicados.

## Estrutura

```
athlete-coach/
├── scripts/
│   ├── atualizar.py         # entrada do agendador: sync + reprocessa
│   ├── garmin_sync.py       # sync incremental (Personal API ou garminconnect)
│   ├── explorar_personal_api.py  # teste exploratório da API oficial
│   ├── strava_export.py     # OAuth + download das atividades do Strava
│   └── export_context.py    # orquestra a ingestão e gera as saídas
├── src/
│   ├── config.py               # todos os caminhos do projeto
│   ├── ingestion/
│   │   ├── garmin.py           # .fit, CSV e JSON do export GDPR
│   │   ├── garmin_wellness.py  # sono, HRV, estresse, VO2max (export GDPR)
│   │   ├── garmin_api.py       # sync + cache cru + parsers da API do Connect
│   │   ├── garmin_personal.py  # cliente da Personal API (oficial) e cliente híbrido
│   │   ├── catalogo.py         # índice das atividades baixadas (novas/alteradas/removidas)
│   │   ├── classificacao.py    # categoria, ambiente e flags por regra
│   │   └── strava.py           # activities.json
│   └── processing/
│       ├── fontes.py           # junta Garmin + Strava sem duplicar e classifica
│       ├── metrics.py          # CTL/ATL/TSB, resumos, build_tables
│       ├── datasets.py         # loaders dos CSVs para os notebooks
│       ├── features.py         # semana, lags, nulos, correlação
│       └── plots.py            # paleta de zonas, eixo de pace
├── notebooks/
│   ├── 00_overview.ipynb       # inventário e qualidade dos dados
│   ├── 01_wellness.ipynb       # sono, HRV, recuperação
│   ├── 02_activities.ipynb     # volume, zonas, pace × FC
│   └── 03_load_recovery.ipynb  # carga × recuperação
├── dados/
│   ├── garmin/      # export GDPR (manual, mensal)
│   ├── strava/      # activities.json (via script, semanal)
│   └── processed/   # CSVs gerados — sobrescritos a cada run
└── athlete_context.md
```

Os caminhos ficam todos em `src/config.py`, resolvidos a partir de `__file__` e
não do diretório de trabalho — funcionam igual rodando da raiz, de `notebooks/`
ou de qualquer lugar. Não use caminhos relativos como `"dados/processed"`.

## Próximos passos

**Análise (fase atual).** O gargalo é a janela de wellness de 4 meses. Análises
de volume, pace e carga usam os 2 anos completos; qualquer coisa que envolva
sono, HRV ou estresse fica restrita a 81 atividades.

**Relatório semanal (fase 3).** O `strava_export.py` já é incremental, então a
sincronização semanal está resolvida. Falta a comparação entre semanas e o
formato do relatório. O wellness continuaria dependendo do export manual do
Garmin, a menos que se troque por uma API não oficial.

**Preditivo (fase 4).** Com 176 atividades, sendo 126 corridas, e 119 dias de
wellness, o volume é pequeno para modelos com muitos parâmetros. Regressão
sobre tendências e detecção de anomalias (HRV fora da baseline, saltos de
carga) são mais realistas do que previsão de performance.

## Documentação

| Documento | Conteúdo |
|---|---|
| [`docs/relatorio-semanal.md`](docs/relatorio-semanal.md) | Decisões do relatório semanal, pipeline completa e tecnologias do Streamlit |
| `CLAUDE.md` | Referência técnica: unidades do Garmin, merge das fontes, convenções |


## Dependências

`fitparse`, `python-dotenv`, `httpx`. Os notebooks pedem `pandas`, `numpy`,
`matplotlib`, `scipy` e `ipykernel`, instalados pelo extra: `pip install -e ".[notebooks]"`.
