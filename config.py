import os

# 策略配置
THRESHOLD_APY = 8.0
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
USE_PERCENTAGE_SIZING = True  # [推薦] 是否啟用百分比倉位管理 (True=啟用, False=使用下方固定金額)
POSITION_SIZE_PERCENT = 0.45  # 一般倉位佔總資金的比例 (0.45 = 45%，留 10% 緩衝給雙重狙擊)
BIG_SHOT_PERCENT = 0.90       # 超級機會佔總資金的比例 (0.90 = 90%，全倉出擊)
MIN_POSITION_AMOUNT = 10.0    # 最小開倉金額 (低於此值不開倉，避免手續費佔比過高)

SINGLE_SHOT_AMOUNT_10_20 = 50.0 # (備用) 固定金額 - 單獨機會
DOUBLE_TAP_AMOUNT_EACH = 50.0   # (備用) 固定金額 - 雙重機會
BIG_SHOT_AMOUNT = 100.0         # (備用) 固定金額 - 超級機會
MAX_CAPITAL_USAGE_PERCENT = 0.88 # 資金使用率上限 (88%)，超過則停止開新倉

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
