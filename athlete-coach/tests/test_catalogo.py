from datetime import date

from conftest import resumo

from ingestion.catalogo import Catalogo


def test_detecta_nova_igual_e_alterada(tmp_path):
    cat = Catalogo.abrir(tmp_path / "cat.csv")
    assert cat.registrar(resumo(1), fonte="personal") == "nova"
    assert cat.registrar(resumo(1), fonte="personal") == "igual"
    # Campo que não entra no hash (social) não conta como mudança
    assert cat.registrar(resumo(1, kudosCount=3), fonte="personal") == "igual"
    assert cat.registrar(resumo(1, nome="Longão"), fonte="personal") == "alterada"


def test_classifica_pelo_type_key(tmp_path):
    cat = Catalogo.abrir(tmp_path / "cat.csv")
    cat.registrar(resumo(7, tipo="treadmill_running"), fonte="personal")
    linha = cat.linhas["7"]
    assert (linha.categoria, linha.ambiente) == ("corrida", "esteira")


def test_salvar_e_reabrir_preserva_tudo(tmp_path):
    caminho = tmp_path / "cat.csv"
    cat = Catalogo.abrir(caminho)
    cat.registrar(resumo(1), fonte="personal")
    cat.marcar_detalhes("1", tem_fit=True, tem_series=False)
    cat.salvar()
    de_novo = Catalogo.abrir(caminho)
    assert de_novo.linhas["1"] == cat.linhas["1"]
    assert not (tmp_path / "cat.csv.tmp").exists()


def test_removida_so_dentro_da_janela_e_volta_se_reaparecer(tmp_path):
    cat = Catalogo.abrir(tmp_path / "cat.csv")
    cat.registrar(resumo(1, inicio="2026-10-01 10:00:00"), fonte="personal")
    cat.registrar(resumo(2, inicio="2026-08-01 10:00:00"), fonte="personal")

    removidas = cat.marcar_removidas(set(), date(2026, 9, 25), date(2026, 10, 5))
    assert removidas == ["1"]  # a 2 está fora da janela consultada
    assert cat.removidas() == {"1"}

    assert cat.registrar(resumo(1, inicio="2026-10-01 10:00:00"), fonte="personal") == "alterada"
    assert cat.removidas() == set()


def test_marca_dagua(tmp_path):
    cat = Catalogo.abrir(tmp_path / "cat.csv")
    assert cat.marca_dagua(7) is None
    cat.registrar(resumo(1, inicio="2026-10-21 10:00:00"), fonte="personal")
    assert cat.marca_dagua(7) == date(2026, 10, 14)
