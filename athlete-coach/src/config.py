"""
Caminhos do projeto, em um lugar só.

Tudo é resolvido a partir da localização deste arquivo (`__file__`), nunca do
diretório de trabalho. Assim os caminhos valem igual rodando de qualquer lugar:
`python scripts/export_context.py` da raiz, um notebook em notebooks/, ou o
interpretador aberto em outra pasta.

Uso:
    from config import GARMIN_DIR, PROCESSED_DIR

Os módulos de ingestão continuam recebendo o diretório por parâmetro — estas
constantes são só o padrão que os scripts injetam neles.
"""

from pathlib import Path

# src/config.py → src/ → athlete-coach/
ROOT = Path(__file__).resolve().parent.parent

# Dados de entrada
DADOS_DIR = ROOT / "dados"
GARMIN_DIR = DADOS_DIR / "garmin"  # export GDPR (atividades + wellness)
GARMIN_API_DIR = DADOS_DIR / "garmin_api"  # cache cru das respostas da API do Connect
GARMIN_TOKENS = DADOS_DIR / ".garmin_tokens"  # sessão salva do garth — nunca versionar
STRAVA_DIR = DADOS_DIR / "strava"
STRAVA_ACTIVITIES = STRAVA_DIR / "activities.json"
IMG_DIR = ROOT / "img"

# Saídas geradas
PROCESSED_DIR = DADOS_DIR / "processed"  # CSVs do build_tables
ATHLETE_CONTEXT = ROOT / "athlete_context.md"

# Registro da última execução do pipeline. É o que permite a interface dizer
# "estes dados têm 9 dias" em vez de mostrar número velho como se fosse novo.
ESTADO_EXECUCAO = DADOS_DIR / ".ultima_execucao.json"


# Configuração
ENV_FILE = ROOT / ".env"
