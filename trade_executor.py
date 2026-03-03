import os
import time
import hmac
import hashlib
import urllib.request
import urllib.parse
import json
import math
import re
from datetime import datetime
import csv
import config # Import config to use global settings for logging and min amounts

# ==================================================================
# 輔助函數：日誌記錄與通知
# ==================================================================

def _escape_markdown_v2(text: str) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    # In MarkdownV2, these characters must be escaped:
    # _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def log_trade_event(event_type, symbol, side="", usdt_value=0.0, quantity=0.0, price=0.0, fee=0.0, funding_fee=0.0, message=""):
    log_file = config.TRADE_LOG_FILE # Use config for log file path
    file_exists = os.path.exists(log_file)
    
    with open(log_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists: # 寫入標題
            writer.writerow(['Timestamp', 'EventType', 'Symbol', 'Side', 'USDTValue', 'Quantity', 'Price', 'Fee', 'FundingFee', 'Message'])
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
            event_type, symbol, side, usdt_value, quantity, price, fee, funding_fee, message
        ])

def _send_telegram_impl(message, parse_mode=None):
    telegram_user_id = config.TELEGRAM_USER_ID
    telegram_bot_token = config.TELEGRAM_BOT_TOKEN
    
    if 'default_api' in globals() and telegram_user_id:
        try:
            default_api.message(action="send", to=telegram_user_id, message=message)
        except Exception as e:
            print(f"❌ 無法發送 Telegram 通知 (default_api 調用失敗): {e}")
    elif telegram_bot_token and telegram_user_id:
        try:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            payload = {"chat_id": telegram_user_id, "text": message}
            if parse_mode:
                payload['parse_mode'] = parse_mode
            data = urllib.parse.urlencode(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data) # POST request
            with urllib.request.urlopen(req) as response:
                pass
        except Exception as e:
            print(f"❌ 無法發送 Telegram 通知 (HTTP 請求失敗): {e}")
    else:
        print("⚠️ Telegram 通知未配置 (TELEGRAM_USER_ID 或 TELEGRAM_BOT_TOKEN 未在 .env 中設定)，無法發送通知。")

def send_telegram_message(message, parse_mode=None):
    _send_telegram_impl(message, parse_mode)

def send_error_notification(message):
    full_message = f"🚨 [資金費率機器人緊急通知] {message}"
    print(full_message) # 仍然保留 print 到控制台
    _send_telegram_impl(full_message)

# ==================================================================
# 核心交易與 API 交互函數
# ==================================================================

def get_keys():
    # 假設 API Keys 儲存在 .env 檔案中，現在它在專案子目錄中
    script_dir = os.path.dirname(__file__)
    env_path = os.path.join(script_dir, ".env")

    keys = {}
    if not os.path.exists(env_path):
        send_error_notification(f"API Keys 檔案 (.env) 不存在於 {env_path}！請檢查配置。")
        return None, None
    with open(env_path) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                keys[k.strip()] = v.strip()
    api_key = keys.get("BINANCE_API_KEY")
    secret_key = keys.get("BINANCE_SECRET_KEY")
    if not api_key or not secret_key:
        send_error_notification("BINANCE_API_KEY 或 BINANCE_SECRET_KEY 未設定在 .env 檔案中！")
        return None, None
    return api_key, secret_key

def fetch_public(url, retries=3, delay=0.5, silent=False):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            if i == retries - 1 and not silent:
                send_error_notification(f"公共 API 請求失敗 (HTTP {e.code}): {error_body}")
                return None
            time.sleep(delay)
        except Exception as e:
            if i == retries - 1 and not silent:
                send_error_notification(f"公共 API 請求失敗 (未知錯誤): {str(e)}")
                return None
            time.sleep(delay)
    return None

def signed_request(endpoint, params, api_key, secret_key, method="POST", base_url="https://fapi.binance.com", retries=3, delay=0.5, silent=False):
    params['timestamp'] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(secret_key.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    url = f"{base_url}{endpoint}?{query}&signature={signature}"
    
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key}, method=method)
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode()
            # 嚴重複雜錯誤直接返回不重試，並通知
            if "-2010" in err_msg or "-2015" in err_msg: 
                if not silent: send_error_notification(f"簽名 API 請求嚴重錯誤 ({endpoint}): {err_msg}")
                return {"error": True, "msg": err_msg}
            if i == retries - 1:
                if not silent: send_error_notification(f"簽名 API 請求失敗 ({endpoint}): {err_msg}")
                return {"error": True, "msg": err_msg}
            time.sleep(delay)
        except Exception as e:
            if i == retries - 1:
                if not silent: send_error_notification(f"簽名 API 請求失敗 (未知錯誤, {endpoint}): {str(e)}")
                return {"error": True, "msg": str(e)}
            time.sleep(delay)
    return {"error": True, "msg": "Unknown error"}

def get_exchange_info():
    """獲取幣安現貨和合約的交易所資訊，包含交易對的精度、最小交易量等。"""
    spot_info = fetch_public("https://api.binance.com/api/v3/exchangeInfo", silent=True)
    fut_info = fetch_public("https://fapi.binance.com/fapi/v1/exchangeInfo", silent=True)
    return spot_info, fut_info

def check_spot_pair_exists(symbol, spot_exchange_info):
    """檢查現貨交易對是否存在"""
    base_asset = symbol.replace('USDT', '')
    if base_asset == 'XAU': # 特殊處理 PAXGUSDT
        return any(s['symbol'] == 'PAXGUSDT' for s in spot_exchange_info['symbols'])
    return any(s['symbol'] == symbol for s in spot_exchange_info['symbols'])

def get_precision_from_step_size(step_size_str):
    step_size_str = step_size_str.rstrip('0') # 去除尾隨零，例如 "0.010000" -> "0.01"
    if '.' in step_size_str:
        return len(step_size_str.split('.')[1])
    return 0

def calculate_spot_fee(fills, price, symbol):
    """從成交明細計算現貨手續費 (換算為 USDT)"""
    total_fee = 0.0
    base_asset = symbol.replace('USDT', '')
    if symbol == 'PAXGUSDT': base_asset = 'PAXG' # 特殊處理
    
    bnb_price = 0.0 
    
    for fill in fills:
        comm = float(fill['commission'])
        asset = fill['commissionAsset']
        
        if asset == 'USDT':
            total_fee += comm
        elif asset == base_asset:
            total_fee += comm * float(fill['price'])
        elif asset == 'BNB':
            if bnb_price == 0.0:
                p = fetch_public("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT")
                if p: bnb_price = float(p['price'])
            if bnb_price > 0:
                total_fee += comm * bnb_price
    return total_fee

def get_futures_fee(symbol, order_id, api_key, secret_key):
    """查詢合約成交紀錄以獲取手續費"""
    for i in range(5): # 最多重試 5 次
        trades = signed_request("/fapi/v1/userTrades", {"symbol": symbol, "orderId": order_id}, api_key, secret_key, method="GET")
        if isinstance(trades, list) and trades: # 確保 trades 是非空列表
            total_fee = 0.0
            for t in trades:
                total_fee += float(t['commission'])
            return total_fee
        
        if i < 4: # 最後一次不延遲
            print(f"   ... 未找到合約手續費記錄，將在 0.5 秒後重試 ({i+1}/5)")
            time.sleep(0.5)

    print(f"   ⚠️ 警告: 多次嘗試後仍無法獲取訂單 {order_id} 的手續費，將記錄為 0。")
    return 0.0

def execute_hedge_safe(symbol, amount_usdt, api_key, secret_key, spot_info_raw, fut_info_raw, min_order_usdt=config.BINANCE_MIN_ORDER_USDT):
    spot_symbol = symbol
    if symbol == 'XAUUSDT': spot_symbol = 'PAXGUSDT'
    
    print(f"🔫 [安全狙擊] 執行對沖: {symbol} (${amount_usdt})...")

    # 解析精度、最小數量、步進單位
    s_info = next((s for s in spot_info_raw['symbols'] if s['symbol'] == spot_symbol), None)
    f_info = next((s for s in fut_info_raw['symbols'] if s['symbol'] == symbol), None)
    
    if not s_info or not f_info: 
        error_msg = f"找不到交易對信息: {symbol}。取消下單。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"交易失敗: {error_msg}")
        return False

    spot_qty_prec = 0
    spot_min_qty = 0
    spot_step_size = 0
    for f in s_info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            spot_min_qty = float(f['minQty'])
            spot_step_size = float(f['stepSize'])
            spot_qty_prec = get_precision_from_step_size(f['stepSize'])
            break

    fut_qty_prec = 0
    fut_min_qty = 0
    fut_step_size = 0
    for f in f_info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            fut_min_qty = float(f['minQty'])
            fut_step_size = float(f['stepSize'])
            fut_qty_prec = get_precision_from_step_size(f['stepSize'])
            break

    # 獲取價格
    price_data = fetch_public(f"https://api.binance.com/api/v3/ticker/price?symbol={spot_symbol}", silent=True)
    if not price_data: 
        error_msg = f"無法獲取價格: {spot_symbol}。取消下單。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"交易失敗: {error_msg}")
        return False
    price = float(price_data['price'])

    # 計算數量
    raw_qty = amount_usdt / price
    
    # 確保數量符合 LOT_SIZE 規則
    # 對現貨數量進行調整
    spot_qty_adjusted = math.floor(raw_qty / spot_step_size) * spot_step_size
    spot_qty_adjusted = round(spot_qty_adjusted, spot_qty_prec)

    # 對合約數量進行調整
    fut_qty_adjusted = math.floor(raw_qty / fut_step_size) * fut_step_size
    fut_qty_adjusted = round(fut_qty_adjusted, fut_qty_prec)
    
    # 取兩者較小值，並確保不小於最小下單量
    final_qty = min(spot_qty_adjusted, fut_qty_adjusted)

    if final_qty < spot_min_qty or final_qty < fut_min_qty:
        error_msg = f"計算出的數量 {final_qty} 低於最小下單量 (Spot:{spot_min_qty}, Fut:{fut_min_qty})，取消下單。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"交易失敗: {error_msg}")
        return False

    if final_qty * price < min_order_usdt:
        error_msg = f"金額過小 (${final_qty * price:.2f})，低於最小交易額限制 ({min_order_usdt:.2f} USDT)，取消下單。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"交易失敗: {error_msg}")
        return False

    print(f"⚖️ 最終下單數量: {final_qty} {spot_symbol}")

    # 1. 買現貨
    print(f"   -> 買入現貨 {final_qty} {spot_symbol}...")
    spot_res = signed_request("/api/v3/order", 
        {"symbol": spot_symbol, "side": "BUY", "type": "MARKET", "quantity": final_qty}, 
        api_key, secret_key, method="POST", base_url="https://api.binance.com", silent=True)
    
    if "orderId" not in spot_res:
        error_msg = f"現貨買入失敗: {spot_res.get('msg', spot_res)}"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"交易失敗: {error_msg}")
        return False
    # 嘗試從現貨成交結果中獲取實際的 USDT 價值、數量和費用
    spot_executed_qty = float(spot_res.get('executedQty', 0.0))
    spot_cummulative_quote_qty = float(spot_res.get('cummulativeQuoteQty', 0.0))
    # 計算真實成交均價，如果成交量為0則使用下單前價格
    actual_spot_price = spot_cummulative_quote_qty / spot_executed_qty if spot_executed_qty > 0 else price
    spot_fee = calculate_spot_fee(spot_res.get('fills', []), actual_spot_price, spot_symbol)
    log_trade_event("Open_Spot", spot_symbol, side="buy", usdt_value=spot_cummulative_quote_qty, quantity=spot_executed_qty, price=actual_spot_price, fee=spot_fee, message=f"ID:{spot_res['orderId']}")
    print(f"   ✅ 現貨成交! (ID: {spot_res['orderId']})")
    
    # 2. 空合約
    print(f"   -> 做空合約...")
    set_leverage_res = signed_request("/fapi/v1/leverage", {"symbol": symbol, "leverage": 1}, api_key, secret_key, method="POST", silent=True)
    # 槓桿設定失敗不一定是致命錯誤，可能只是已經是 1x，所以只打印警告
    if "code" in set_leverage_res and set_leverage_res.get("code") != 200:
        print(f"   ⚠️ 槓桿設定失敗或已是目標值: {set_leverage_res}")

    fut_res = signed_request("/fapi/v1/order", 
        {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": spot_executed_qty, "reduceOnly": "false"}, 
        api_key, secret_key, method="POST", silent=True)
        
    if "orderId" in fut_res and float(fut_res.get('executedQty', 0.0)) > 0:
        fut_executed_qty = float(fut_res.get('executedQty', 0.0))
        fut_cum_quote = float(fut_res.get('cumQuote', 0.0))
        # 計算真實成交均價
        actual_fut_price = fut_cum_quote / fut_executed_qty if fut_executed_qty > 0 else price
        fut_fee = get_futures_fee(symbol, fut_res['orderId'], api_key, secret_key)
        log_trade_event("Open_Futures", symbol, side="sell", usdt_value=fut_cum_quote, quantity=fut_executed_qty, price=actual_fut_price, fee=fut_fee, message=f"ID:{fut_res['orderId']}")
        print(f"   ✅ 合約空單成交! (ID: {fut_res['orderId']})")
        print(f"🎉 對沖策略部署成功！")
        return True
    else:
        # 3. 回滾
        error_msg = f"合約做空失敗: {fut_res.get('msg', fut_res)}"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"交易失敗: {error_msg} 啟動回滾機制！")
        
        print(f"⚠️ 偵測到單邊風險！啟動【安全回滾機制 (Rollback)】...")
        print(f"🔄 正在賣出剛剛買入的現貨 ({final_qty} {spot_symbol})...")
        
        rollback_res = signed_request("/api/v3/order", 
            {"symbol": spot_symbol, "side": "SELL", "type": "MARKET", "quantity": spot_executed_qty}, 
            api_key, secret_key, method="POST", base_url="https://api.binance.com", silent=True)
        
        if "orderId" in rollback_res:
            rollback_executed_qty = float(rollback_res.get('executedQty', 0.0))
            rollback_cummulative_quote_qty = float(rollback_res.get('cummulativeQuoteQty', 0.0))
            # 計算真實成交均價
            actual_rollback_price = rollback_cummulative_quote_qty / rollback_executed_qty if rollback_executed_qty > 0 else price
            rollback_fee = calculate_spot_fee(rollback_res.get('fills', []), actual_rollback_price, spot_symbol)
            log_trade_event("Rollback_Spot", spot_symbol, side="sell", usdt_value=rollback_cummulative_quote_qty, quantity=rollback_executed_qty, price=actual_rollback_price, fee=rollback_fee, message=f"ID:{rollback_res['orderId']}")
            print(f"   ✅ 回滾成功！現貨已賣出，資金已安全撤回。")
            send_error_notification(f"交易失敗，但回滾成功。資金已安全撤回。")
        else:
            critical_error_msg = f"回滾失敗！請立即手動賣出現貨！ Error: {rollback_res.get('msg', rollback_res)}"
            print(f"   ❌❌❌ {critical_error_msg}")
            send_error_notification(f"🚨 交易失敗 & 回滾失敗！請立即手動賣出現貨！ {critical_error_msg}")
        return False

def get_balances(api_key, secret_key):
    spot_acct = signed_request("/api/v3/account", {}, api_key, secret_key, method="GET", base_url="https://api.binance.com")
    spot_free = 0.0
    if "balances" in spot_acct and not "error" in spot_acct:
        for b in spot_acct['balances']:
            if b['asset'] == 'USDT': spot_free = float(b['free'])
            
    fut_acct = signed_request("/fapi/v2/account", {}, api_key, secret_key, method="GET")
    fut_free = 0.0
    if "availableBalance" in fut_acct and not "error" in fut_acct: fut_free = float(fut_acct['availableBalance'])
        
    return spot_free, fut_free

def transfer_funds(amount, transfer_type, api_key, secret_key, min_transfer_amount=config.TRANSFER_MIN_AMOUNT):
    # transfer_type: "MAIN_UMFUTURE" (Spot to Futures), "UMFUTURE_MAIN" (Futures to Spot)
    if amount <= 0:
        return True # 無需劃轉
    
    if amount < min_transfer_amount: 
        print(f"   ⚠️ 劃轉金額 {amount:.2f} USDT 過小，跳過劃轉。")
        return True

    print(f"   🔄 執行資金劃轉: {transfer_type} 轉移 {amount:.2f} USDT...")
    transfer_res = signed_request("/sapi/v1/asset/transfer", {
        "asset": "USDT",
        "amount": round(amount, 2), # 劃轉金額通常只需要兩位小數
        "type": transfer_type
    }, api_key, secret_key, method="POST", base_url="https://api.binance.com", silent=True) # 注意: /sapi/v1 是在 api.binance.com

    if "tranId" in transfer_res:
        print(f"   ✅ 劃轉成功! Transaction ID: {transfer_res['tranId']}")
        log_trade_event("Fund_Transfer", "USDT", side="transfer", usdt_value=amount, message=f"Type:{transfer_type}, ID:{transfer_res['tranId']}") # Updated log_trade_event
        return True
    else:
        error_msg = f"資金劃轉失敗! Error: {transfer_res.get('msg', transfer_res)}"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"資金劃轉失敗: {error_msg}")
        return False

def check_and_balance_funds(api_key, secret_key, needed_spot, needed_fut, min_transfer_amount=config.TRANSFER_MIN_AMOUNT):
    current_spot_free, current_fut_free = get_balances(api_key, secret_key)
    
    # 檢查總資金是否足夠進行本次狙擊
    # 增加 1% 的緩衝區 (Buffer)，防止市價單滑點導致資金不足而下單失敗
    buffer_multiplier = 1.01 
    needed_spot_buffered = needed_spot * buffer_multiplier
    needed_fut_buffered = needed_fut * buffer_multiplier
    total_needed_buffered = needed_spot_buffered + needed_fut_buffered

    if (current_spot_free + current_fut_free) < total_needed_buffered:
        error_msg = f"總資金不足 (${current_spot_free + current_fut_free:.1f})，需要 ${total_needed_buffered:.1f} (含緩衝)。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"狙擊取消: {error_msg}")
        return False, current_spot_free, current_fut_free # 返回原始餘額

    print(f"   💸 正在檢查並執行精準資金平衡...")

    # 計算各帳戶資金缺口
    spot_deficit = max(0, needed_spot_buffered - current_spot_free)
    fut_deficit = max(0, needed_fut_buffered - current_fut_free)

    transfer_successful = True
    # 情況 1: 現貨不足，需要從合約劃轉
    if spot_deficit > 0:
        # 檢查合約帳戶是否有足夠的"閒錢" (扣除自身所需後)
        if current_fut_free - needed_fut_buffered >= spot_deficit:
            print(f"   -> 現貨資金不足 ${spot_deficit:.2f}。")
            if not transfer_funds(spot_deficit, "UMFUTURE_MAIN", api_key, secret_key, min_transfer_amount):
                transfer_successful = False
        else:
            error_msg = f"合約帳戶資金不足以支援現貨帳戶 (需轉 ${spot_deficit:.2f})。"
            print(f"   ❌ {error_msg}")
            send_error_notification(f"狙擊取消: {error_msg}")
            transfer_successful = False
    
    # 情況 2: 合約不足，需要從現貨劃轉
    elif fut_deficit > 0:
        # 檢查現貨帳戶是否有足夠的"閒錢"
        if current_spot_free - needed_spot_buffered >= fut_deficit:
            print(f"   -> 合約資金不足 ${fut_deficit:.2f}。")
            if not transfer_funds(fut_deficit, "MAIN_UMFUTURE", api_key, secret_key, min_transfer_amount):
                transfer_successful = False
        else:
            error_msg = f"現貨帳戶資金不足以支援合約帳戶 (需轉 ${fut_deficit:.2f})。"
            print(f"   ❌ {error_msg}")
            send_error_notification(f"狙擊取消: {error_msg}")
            transfer_successful = False

    if not transfer_successful:
        # 如果任何劃轉失敗，則直接返回失敗
        return False, current_spot_free, current_fut_free # 返回原始餘額

    # 重新獲取劃轉後的最終資金
    final_spot_free, final_fut_free = get_balances(api_key, secret_key)
    
    # 最終檢查
    if final_spot_free >= needed_spot_buffered and final_fut_free >= needed_fut_buffered:
        print(f"   ✅ 資金已準備就緒！現貨可用: ${final_spot_free:.2f}, 合約可用: ${final_fut_free:.2f}")
        return True, final_spot_free, final_fut_free
    else:
        # 這種情況理論上不應發生，除非有延遲或劃轉金額過小被跳過
        error_msg = f"資金平衡後仍不滿足需求 (現貨餘額:${final_spot_free:.1f}, 合約餘額:${final_fut_free:.1f})。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"狙擊取消: {error_msg}")
        return False, final_spot_free, final_fut_free

def get_all_futures_positions(api_key, secret_key):
    positions = signed_request("/fapi/v2/positionRisk", {}, api_key, secret_key, method="GET")
    active_positions = []
    if isinstance(positions, list) and not "error" in positions:
        for p in positions:
            amt = float(p['positionAmt'])
            if amt != 0:
                active_positions.append({'symbol': p['symbol'], 'amount': abs(amt)})
    return active_positions

def close_position(symbol, amount, api_key, secret_key, spot_info_raw): # Added spot_info_raw parameter
    print(f"🚨 正在執行自動平倉: {symbol} (數量: {amount})...")
    fut_close_res = signed_request("/fapi/v1/order", 
        {"symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": amount, "reduceOnly": "true"}, 
        api_key, secret_key, method="POST", silent=True)
    
    # 處理市價單可能處於 NEW 狀態的情況 (尚未完全成交)
    if "orderId" in fut_close_res and float(fut_close_res.get('executedQty', 0.0)) == 0:
        print(f"   ⏳ 合約平倉訂單已提交 (ID: {fut_close_res['orderId']}) 但尚未成交，正在等待成交確認...")
        for _ in range(5): # 最多等待 5 秒
            time.sleep(1)
            check_res = signed_request("/fapi/v1/order", 
                {"symbol": symbol, "orderId": fut_close_res['orderId']}, 
                api_key, secret_key, method="GET", silent=True)
            if "status" in check_res:
                fut_close_res = check_res # 更新訂單資訊
                if float(fut_close_res.get('executedQty', 0.0)) > 0:
                    print(f"   ✅ 訂單已成交！")
                    break

    if "orderId" not in fut_close_res or float(fut_close_res.get('executedQty', 0.0)) == 0:
        error_msg = f"合約平倉失敗: {fut_close_res.get('msg', fut_close_res)}"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"平倉失敗: {error_msg}")
        return
    fut_executed_qty = float(fut_close_res.get('executedQty', 0.0))
    fut_cum_quote = float(fut_close_res.get('cumQuote', 0.0))
    # 計算真實成交均價
    actual_fut_price = fut_cum_quote / fut_executed_qty if fut_executed_qty > 0 else 0.0
    fut_fee = get_futures_fee(symbol, fut_close_res['orderId'], api_key, secret_key)
    log_trade_event("Close_Futures", symbol, side="buy", usdt_value=fut_cum_quote, quantity=fut_executed_qty, price=actual_fut_price, fee=fut_fee, message=f"ID:{fut_close_res['orderId']}")
    print(f"   ✅ 合約已平倉 {fut_executed_qty}。")

    spot_symbol = symbol
    if symbol == 'XAUUSDT': spot_symbol = 'PAXGUSDT'
    
    # 在賣出現貨前獲取即時價格
    price_data = fetch_public(f"https://api.binance.com/api/v3/ticker/price?symbol={spot_symbol}", silent=True)
    if not price_data:
        error_msg = f"平倉後無法獲取現貨價格 {spot_symbol}，無法賣出。"
        print(f"   ❌ {error_msg}")
        send_error_notification(f"平倉失敗: {error_msg}")
        return
    price = float(price_data['price'])

    # 使用合約實際平倉數量作為現貨賣出目標，而不是查詢帳戶總餘額
    spot_qty_to_sell = fut_executed_qty
    
    if spot_qty_to_sell > 0:
        s_info = next((s for s in spot_info_raw['symbols'] if s['symbol'] == spot_symbol), None)
        if s_info:
            spot_qty_prec = 0
            spot_min_qty = 0
            spot_step_size = 0
            for f in s_info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    spot_min_qty = float(f['minQty'])
                    spot_step_size = float(f['stepSize'])
                    spot_qty_prec = get_precision_from_step_size(f['stepSize'])
                    break
            
            # 調整現貨數量以符合精度和最小下單量
            safe_qty = math.floor(spot_qty_to_sell / spot_step_size) * spot_step_size
            safe_qty = round(safe_qty, spot_qty_prec)
            
            if safe_qty >= spot_min_qty: # 確保調整後的數量符合最小下單量
                spot_sell_res = signed_request("/api/v3/order", 
                    {"symbol": spot_symbol, "side": "SELL", "type": "MARKET", "quantity": safe_qty}, 
                    api_key, secret_key, method="POST", base_url="https://api.binance.com", silent=True)
                if "orderId" in spot_sell_res:
                    spot_executed_qty_sell = float(spot_sell_res.get('executedQty', 0.0))
                    spot_cummulative_quote_qty_sell = float(spot_sell_res.get('cummulativeQuoteQty', 0.0))
                    # 計算真實成交均價
                    actual_spot_price_sell = spot_cummulative_quote_qty_sell / spot_executed_qty_sell if spot_executed_qty_sell > 0 else price
                    spot_fee_sell = calculate_spot_fee(spot_sell_res.get('fills', []), actual_spot_price_sell, spot_symbol)
                    log_trade_event("Close_Spot", spot_symbol, side="sell", usdt_value=spot_cummulative_quote_qty_sell, quantity=spot_executed_qty_sell, price=actual_spot_price_sell, fee=spot_fee_sell, message=f"ID:{spot_sell_res['orderId']}")
                    print("   ✅ 現貨已賣出。")
                else:
                    error_msg = f"現貨賣出失敗: {spot_sell_res.get('msg', spot_sell_res)}"
                    print(f"   ❌ {error_msg}")
                    send_error_notification(f"平倉後現貨賣出失敗: {error_msg}")
            else:
                print(f"   ⚠️ 應賣出現貨數量 {spot_qty_to_sell:.4f} 調整後 ({safe_qty:.4f}) 不滿足最小下單量 ({spot_min_qty:.4f})，跳過賣出。")
        else:
            print(f"   ❌ 無法獲取現貨 {spot_symbol} 交易對信息，無法賣出。")
    else:
        print("   ℹ️ 合約平倉數量為 0，無需賣出現貨。")
    print("🏁 自動平倉程序結束。")

def scan_top_opportunities(spot_exchange_info):
    rates = fetch_public("https://fapi.binance.com/fapi/v1/premiumIndex")
    tickers = fetch_public("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not rates or not tickers: return []

    vol_map = {t['symbol']: float(t['quoteVolume']) for t in tickers}
    candidates = []
    for item in rates:
        s = item['symbol']
        if not s.endswith('USDT'): continue
        if s in config.EXCLUDE_SYMBOLS: continue # 使用 config 中的 EXCLUDE_SYMBOLS
        vol = vol_map.get(s, 0)
        if vol < config.MIN_VOLUME_USDT: continue # 使用 config 中的 MIN_VOLUME_USDT
        if not check_spot_pair_exists(s, spot_exchange_info): continue
        
        rate = float(item['lastFundingRate'])
        apy = rate * 3 * 365 * 100
        
        if apy > 0:
            candidates.append({'symbol': s, 'apy': apy, 'vol': vol})
            
    candidates.sort(key=lambda x: x['apy'], reverse=True)
    return candidates[:5]

def close_all_active_positions(api_key, secret_key, spot_info_raw): # Added spot_info_raw parameter
    print("🚨 正在平倉所有活躍的對沖倉位...")
    active_positions = get_all_futures_positions(api_key, secret_key)
    success = True
    if active_positions:
        for pos in active_positions:
            symbol = pos['symbol']
            amount = pos['amount']
            print(f"   - 正在平倉 {symbol}，數量 {amount}...")
            try:
                close_position(symbol, amount, api_key, secret_key, spot_info_raw)
            except Exception as e:
                send_error_notification(f"平倉所有倉位時，平倉 {symbol} 失敗: {e}")
                success = False
        print("✅ 所有活躍倉位已嘗試平倉完成。")
    else:
        print("🟢 目前沒有任何活躍的對沖倉位需要平倉。")
    return success
