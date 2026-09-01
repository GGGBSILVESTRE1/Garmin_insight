# Dados processados

Gerado por `processing.metrics.build_tables()`. Não editar à mão — o conteúdo é sobrescrito a cada execução de `scripts/export_context.py`.

## Arquivos

| Arquivo | Linhas | Conteúdo |
|---|---|---|
| `activities.csv` | 176 | Uma linha por treino (Garmin + Strava), com TSS e todas as métricas específicas de cada fonte. |
| `activity_hr_zones.csv` | 574 | Tempo em cada zona de frequência cardíaca, por atividade (formato longo). |
| `activity_power_zones.csv` | 270 | Tempo em cada zona de potência, por atividade (formato longo). |
| `strength_sets.csv` | 111 | Séries de musculação por exercício: séries, repetições, volume e carga máxima. |
| `training_load_daily.csv` | 366 | Série diária de TSS, CTL (fitness), ATL (fadiga) e TSB (forma). |
| `weekly_summary.csv` | 52 | Agregado semanal: volume, duração, pace médio e distribuição por esporte. |
| `wellness_daily.csv` | 119 | Uma linha por dia: sono e estágios, HRV, FC de repouso, estresse, passos, SpO2 e VO2max. |

## Cobertura

- **Atividades:** 176 (strava: 94, strava+garmin: 82)
- **Período:** 2024-06-02 a 2026-08-06
- **Dias de wellness:** 119 (2026-04-08 a 2026-08-04)

## Convenções de unidade

Valores já convertidos para unidades reais na ingestão:

- Garmin `distance` e campos de elevação vêm em **centímetros** no export GDPR
- Garmin `duration` e os campos `*TimeInZone_*` vêm em **milissegundos**
- Garmin `avgSpeed`/`maxSpeed` vêm a 1/10 do m/s real
- Garmin `calories`/`bmrCalories` vêm a 10x o valor real
- O export do Strava **não traz calorias** — a coluna só é preenchida em treinos Garmin

*Gerado em 17/08/2026 18:13*