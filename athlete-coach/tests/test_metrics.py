"""TSS e CTL/ATL: as duas correções desta rodada."""
from datetime import datetime, timedelta

from conftest import atividade

from processing.metrics import FC_REFERENCIA, TrainingLoadMetrics


def test_corrida_com_potencia_usa_fc_e_nao_ftp_de_bike():
    # O caso real de 28/09/2026: esteira, 47 min, 525 W, FC 153 → dava TSS 344
    esteira = atividade(dur_s=46.8 * 60, fc=153.0, potencia=525.0,
                        activity_type="treadmill_running")
    tss = TrainingLoadMetrics([esteira]).training_stress_score(esteira)
    esperado = round(46.8 / 60 * (153.0 / FC_REFERENCIA) ** 2 * 100, 1)
    assert tss == esperado
    assert tss < 60


def test_corrida_com_e_sem_potencia_ficam_na_mesma_escala():
    com = atividade(potencia=290.0)
    sem = atividade(potencia=None)
    carga = TrainingLoadMetrics([com, sem])
    assert carga.training_stress_score(com) == carga.training_stress_score(sem)


def test_bike_continua_usando_potencia():
    bike = atividade(sport="Ride", potencia=250.0, fc=120.0)
    assert TrainingLoadMetrics([bike]).training_stress_score(bike) == 100.0


def test_ctl_aquece_com_historico_anterior_a_janela():
    hoje = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    # Treino diário de 200 a 100 dias atrás: fora de uma janela de 30 dias
    antigas = [
        atividade(inicio=(hoje - timedelta(days=d)).isoformat(), aid=str(d))
        for d in range(100, 200)
    ]
    serie = TrainingLoadMetrics(antigas).compute_ctl_atl(window_days=30)
    primeiro = serie[min(serie)]
    # Antes da correção o CTL começava em zero no início da janela
    assert primeiro["ctl"] > 0
    assert len(serie) == 31
