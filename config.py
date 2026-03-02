import os

# 策略配置
THRESHOLD_APY = 5.0
MIN_VOLUME_USDT = 50_000_000 # 最小 24h 交易量，用於篩選市場機會

SNIPER_MODE = True
BIG_SHOT_THRESHOLD = 20.0    # 超級機會 APY 門檻 (單發 $100)
DOUBLE_TAP_MIN = 10.0      # 雙重/單獨機會 APY 門檻下限
DOUBLE_TAP_MAX = 20.0      # 雙重/單獨機會 APY 門檻上限

# 排除穩定幣，避免低收益佔用資金
EXCLUDE_SYMBOLS = [
    'USDCUSDT', 'BUSDUSDT', 'USDPUSDT', 'TUSDUSDT', 'FDUSDUSDT', 'PYUSDUSDT'
]

# 交易金額配置
SINGLE_SHOT_AMOUNT_10_20 = 50.0 # 單獨機會 (10-20% APY) 下單金額
DOUBLE_TAP_AMOUNT_EACH = 50.0   # 雙重機會，每個標的下單金額
BIG_SHOT_AMOUNT = 100.0         # 超級機會 (>20% APY) 下單金額

# 其他配置
BINANCE_MIN_ORDER_USDT = 5.5  # 幣安最小訂單金額 (通常是 5 USDT，為保險起見設 5.5)
TRANSFER_MIN_AMOUNT = 0.2     # 幣安資金劃轉最小金額 (通常是 0.1 USDT，為保險起見設 0.2)

# 日誌文件路徑
TRADE_LOG_FILE = os.path.join(os.path.dirname(__file__), 'trade_log.csv')

# Telegram 通知配置
TELEGRAM_USER_ID = os.getenv('TELEGRAM_USER_ID')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# 嘗試從 .env 讀取 (如果環境變數未設定)
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path) and (TELEGRAM_USER_ID is None or TELEGRAM_BOT_TOKEN is None):
    with open(_env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip()
            if k == 'TELEGRAM_USER_ID' and TELEGRAM_USER_ID is None:
                TELEGRAM_USER_ID = v
            elif k == 'TELEGRAM_BOT_TOKEN' and TELEGRAM_BOT_TOKEN is None:
                TELEGRAM_BOT_TOKEN = v
