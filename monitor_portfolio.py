import os
import time
import sys
from datetime import datetime

# 導入外部配置
import config
# 導入交易執行和資金管理模組
from trade_executor import (
    get_keys, fetch_public, signed_request, get_exchange_info, 
    check_spot_pair_exists, get_balances, check_and_balance_funds, 
    get_all_futures_positions, close_position, execute_hedge_safe, 
    log_trade_event, send_error_notification, scan_top_opportunities, 
    close_all_active_positions, send_telegram_message, _escape_markdown_v2
)

def main():
    # --- Lock File 機制 ---
    lock_file_path = os.path.join(os.path.dirname(__file__), '.monitor.lock')
    if os.path.exists(lock_file_path):
        # 檢查 lock 檔案的創建時間，如果超過一定時間（例如 15 分鐘），可能意味著上次執行異常中斷
        file_mod_time = os.path.getmtime(lock_file_path)
        if (time.time() - file_mod_time) > 900: # 15 minutes
            print("⚠️ 發現一個舊的鎖定檔案，可能上次執行未正常結束。正在移除並繼續...")
            os.remove(lock_file_path)
        else:
            print("🔄 另一個監控程序正在運行，本次執行跳過。")
            sys.exit()
    
    # 創建鎖定檔案
    with open(lock_file_path, 'w') as f:
        f.write(str(os.getpid()))

    api_key, secret_key = get_keys()
    if not api_key:
        os.remove(lock_file_path) # 獲取 key 失敗時也要移除 lock
        return 
    # 獲取交易所資訊 (一次性，供後續精度和交易對檢查使用)
    spot_info, fut_info = get_exchange_info()
    if not spot_info or not fut_info:
        send_error_notification("無法取得交易所資訊，請檢查網路或幣安API狀態！")
        os.remove(lock_file_path)
        return
    
    # Console printing function for detailed logs
    def p(text=""):
        print(text)

    # --- Console Output Start ---
    p(f"===== 🦅 資金費率投資組合報告 =====")
    p(f"🕒 時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    p(f"========================================\n")

    # Flag to track if any important event occurred
    important_event_occurred = False

    # --- Data Gathering and Logic ---
    # 1. 持倉監控
    active_positions = get_all_futures_positions(api_key, secret_key)
    portfolio_max_apy = 0.0
    portfolio_min_apy = float('inf') # 新增：用於追蹤組合中的最低 APY
    active_symbols = []
    total_futures_notional = 0.0 # 用於計算總持倉價值
    positions_closed_in_loop = False # 標記是否在迴圈中執行了平倉

    # [優化] 批量獲取所有幣種的資金費率資料，避免迴圈中多次 API 請求
    all_premium_indices = fetch_public("https://fapi.binance.com/fapi/v1/premiumIndex")
    premium_index_map = {item['symbol']: item for item in all_premium_indices} if all_premium_indices else {}

    if active_positions:
        p("🟢 現有對沖倉位績效:")
        for pos in active_positions:
            symbol = pos['symbol']
            active_symbols.append(symbol)
            # [優化] 從 Map 中直接獲取，不發送網路請求
            premium_index = premium_index_map.get(symbol)
            
            # 累加持倉名目價值 (用於計算資金使用率)
            mark_price = float(premium_index['markPrice']) if premium_index and 'markPrice' in premium_index else 0.0
            total_futures_notional += pos['amount'] * mark_price
            
            curr_apy = float(premium_index['lastFundingRate']) * 3 * 365 * 100 if premium_index else 0
            if curr_apy > portfolio_max_apy: portfolio_max_apy = curr_apy
            if curr_apy < portfolio_min_apy: portfolio_min_apy = curr_apy
            pos['apy'] = curr_apy # 記錄 APY 供後續智能換倉使用
            
            # 更新資金費入帳的日誌記錄
            incomes = signed_request("/fapi/v1/income", {"symbol": symbol, "incomeType": "FUNDING_FEE", "limit": 1}, api_key, secret_key, method="GET")
            if isinstance(incomes, list) and incomes:
                last_income = incomes[0]
                last_income_amount = float(last_income['income'])
                last_income_id = last_income['tranId']
                last_income_str = f"${last_income_amount:.4f}" if last_income_amount != 0.0 else "N/A"
                
                # 檢查是否為新的、非零的資金費用，避免重複記錄
                # 這裡假設 log_trade_event 內部或外部會有機制檢查 ID 唯一性，我們先記錄 ID
                # 為了簡化，我們先只記錄非零的費用，並附上唯一 ID
                if last_income_amount != 0.0:
                    log_trade_event("Funding_Fee", symbol, side="funding", usdt_value=last_income_amount, quantity=0.0, price=0.0, fee=0.0, funding_fee=last_income_amount, message=f"APY:{curr_apy:.2f}%, ID:{last_income_id}")
            else:
                last_income_str = "N/A"

            status = "✅ 正常"
            if curr_apy < 0: 
                status = "🚨 緊急: 負費率！自動平倉..."
                p(f"   - {symbol}: 最新入帳 {last_income_str} | 年化 {curr_apy:.2f}% | 狀態: {status}")
                # 更新 close_position 調用，傳遞 spot_info
                close_position(symbol, pos['amount'], api_key, secret_key, spot_info)
                important_event_occurred = True
                positions_closed_in_loop = True
                continue
            elif curr_apy < config.THRESHOLD_APY: status = "⚠️ 警告: 收益過低"
            
            p(f"   - {symbol}: 最新入帳 {last_income_str} | 年化 {curr_apy:.2f}% | 狀態: {status}")
        p(f"----------------------------------------")
    else:
        p("🟢 (目前無任何對沖倉位)\n")
        # 優化點 3.3: 調整 portfolio_max_apy 初始化與 diff_str 顯示
        portfolio_max_apy = float('-inf') # 如果沒有持倉，則視為所有機會都高於當前收益
        portfolio_min_apy = float('inf')

    # --- 狀態刷新機制 ---
    # 如果在監控過程中發生了平倉 (例如負費率)，需要刷新狀態以避免後續邏輯使用過期數據
    if positions_closed_in_loop:
        p("   🔄 偵測到倉位變動 (負費率平倉)，重新獲取持倉與資金狀態...")
        active_positions = get_all_futures_positions(api_key, secret_key)
        # 重置狀態變數
        active_symbols = []
        total_futures_notional = 0.0
        portfolio_max_apy = 0.0 if active_positions else float('-inf')
        portfolio_min_apy = float('inf') if not active_positions else portfolio_min_apy # 保持之前的 min_apy，或在清空後重置
        
        if active_positions:
            # 如果發生狀態刷新，重新獲取一次最新的批量資料
            all_premium_indices = fetch_public("https://fapi.binance.com/fapi/v1/premiumIndex")
            premium_index_map = {item['symbol']: item for item in all_premium_indices} if all_premium_indices else {}
            
            for pos in active_positions:
                symbol = pos['symbol']
                active_symbols.append(symbol)
                # 快速重新計算名目價值和 Max APY
                premium_index = premium_index_map.get(symbol)
                mark_price = float(premium_index['markPrice']) if premium_index and 'markPrice' in premium_index else 0.0
                total_futures_notional += pos['amount'] * mark_price
                curr_apy = float(premium_index['lastFundingRate']) * 3 * 365 * 100 if premium_index else 0
                if curr_apy > portfolio_max_apy: portfolio_max_apy = curr_apy
                if curr_apy < portfolio_min_apy: portfolio_min_apy = curr_apy

    # 2. 掃描與狙擊
    p(f"🔍 市場前 5 名優質標的 (24h成交量 > ${config.MIN_VOLUME_USDT/1_000_000:.0f}M)...\n")
    better_ops = scan_top_opportunities(spot_info, all_premium_indices) # [優化] 傳入已獲取的資料
    
    if better_ops:
        for op in better_ops:
            if portfolio_max_apy == float('-inf'):
                diff_str = ""
            else:
                diff = op['apy'] - portfolio_max_apy
                diff_str = f" (🔥 高出 {diff:.1f}%)" if diff > 0 else f" (低於當前 {abs(diff):.1f}%)"
            p(f"{op['symbol']:<12} {op['apy']:.2f}%      ${op['vol']/1_000_000:.0f}M {diff_str}")
        
        # 提前獲取資金狀態，確保變數在 Sniper Mode 關閉時也能用於報表，且使用最新數據
        current_spot_free_initial, current_fut_free_initial = get_balances(api_key, secret_key)
        
        # 狙擊邏輯
        if config.SNIPER_MODE and better_ops:
            # 優化: 先過濾掉已持有的倉位，只關注"可下單"的機會
            # 這樣即使持有市場第一名，也不會因此錯過市場第二名的機會
            available_ops = [op for op in better_ops if op['symbol'] not in active_symbols]
            
            top1 = available_ops[0] if available_ops else None
            top2 = available_ops[1] if len(available_ops) > 1 else None

            # 估算總權益與使用率 (假設 1x 槓桿，已投入資金約為合約名目價值的 2 倍: 1份現貨 + 1份合約保證金)
            total_invested = total_futures_notional * 2
            total_available = current_spot_free_initial + current_fut_free_initial
            total_equity = total_invested + total_available
            usage_ratio = total_invested / total_equity if total_equity > 0 else 0.0

            p(f"   (資金使用率: {usage_ratio*100:.1f}% | 可用資金 Spot:${current_spot_free_initial:.1f} Fut:${current_fut_free_initial:.1f})")

            is_capital_maxed = False
            if usage_ratio >= config.MAX_CAPITAL_USAGE_PERCENT:
                p(f"   ⚠️ 資金使用率已達 {usage_ratio*100:.1f}% (>= {config.MAX_CAPITAL_USAGE_PERCENT*100:.0f}%)，將暫停新增倉位 (僅允許優質換倉)。")
                is_capital_maxed = True

            # --- 動態計算下單金額 ---
            if config.USE_PERCENTAGE_SIZING:
                # 一般倉位: 總權益 * 設定百分比 (例如 45%)
                calc_single_amount = total_equity * config.POSITION_SIZE_PERCENT
                single_shot_amount = max(calc_single_amount, config.MIN_POSITION_AMOUNT)
                
                # 雙重狙擊: 與單發相同
                double_tap_amount = single_shot_amount
                
                # 超級機會: 總權益 * 設定百分比 (例如 90%)
                calc_big_amount = total_equity * config.BIG_SHOT_PERCENT
                big_shot_amount = max(calc_big_amount, config.MIN_POSITION_AMOUNT)
            else:
                single_shot_amount = config.SINGLE_SHOT_AMOUNT_10_20
                double_tap_amount = config.DOUBLE_TAP_AMOUNT_EACH
                big_shot_amount = config.BIG_SHOT_AMOUNT

            # --- 新增邏輯: 已有 $50 倉位，且市場出現雙重機會，則只補一個 $50 --- 
            # 判斷是否只持有一個倉位 (無論金額大小)
            has_one_position = len(active_positions) == 1
            
            if has_one_position and not is_capital_maxed:
                held_symbol = active_positions[0]['symbol']
                # 從可用機會中選出最好的那個 (排除已持有的)
                opportunity_to_take = None
                # 直接使用已經過濾好的 available_ops
                if top1 and config.DOUBLE_TAP_MIN <= top1['apy'] <= config.DOUBLE_TAP_MAX:
                    opportunity_to_take = top1

                if opportunity_to_take:
                    # [修正] 補齊第二倉位時，檢查剩餘資金是否足夠，若不足則自動調整金額
                    total_avail_second = current_spot_free_initial + current_fut_free_initial
                    max_affordable_second = total_avail_second / 2.03
                    if single_shot_amount > max_affordable_second:
                        if max_affordable_second >= config.BINANCE_MIN_ORDER_USDT:
                            p(f"   ⚠️ 資金調整: 可用餘額 (${total_avail_second:.1f}) 不足目標 (${single_shot_amount:.1f})，調整第二倉位金額為 (${max_affordable_second:.1f})...")
                            single_shot_amount = max_affordable_second

                    p(f"\n   🎯 已持有一倉位 ({held_symbol})，市場出現優質新機會 {opportunity_to_take['symbol']} ({opportunity_to_take['apy']:.2f}%)！準備補齊第二個倉位，單發狙擊 ${single_shot_amount:.1f}...")
                    funds_ok, _, _ = check_and_balance_funds(api_key, secret_key, single_shot_amount, single_shot_amount, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok: # 如果資金平衡成功
                        execute_hedge_safe(opportunity_to_take['symbol'], single_shot_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        important_event_occurred = True
                    else:
                        p(f"   ❌ 狙擊 {opportunity_to_take['symbol']} 失敗: 資金不足，取消狙擊。")
                        important_event_occurred = True
                else:
                    p("\n   💤 最佳新機會收益未達單獨狙擊門檻，或無新機會。")
            
            # --- 其他情況：無持倉，或多個持倉，或未滿足上述特殊條件 --- 
            else: 
                # 計算收益差異 (僅在有持倉時計算)
                apy_diff = 0.0
                if top1 and active_positions:
                    apy_diff = top1['apy'] - portfolio_min_apy # 關鍵修改：與組合中表現最差的比較

                # [優化] 提高換倉門檻至 10%，避免因頻繁換倉導致手續費 (約 0.3%) 吃掉利潤
                # 0.3% 手續費 / (5% 年化差 / 365) ≈ 22 天回本。門檻過低會導致頻繁磨損。
                swap_threshold = 10.0
                # 超級機會 (>20%) 或 顯著優於當前最差持倉 (>10% 差異)
                if top1 and (top1['apy'] > config.BIG_SHOT_THRESHOLD or apy_diff > swap_threshold):
                    # 決定目標金額: 
                    # 1. 如果是真正的超級機會 (>20%)，嘗試用大倉位 (Big Shot)
                    # 2. 如果只是因為差異大而換倉 (例如 5% -> 16%)，但未達超級門檻，則保守使用普通倉位，避免小帳戶平倉後不夠錢開大倉
                    target_amount = big_shot_amount
                    if top1['apy'] <= config.BIG_SHOT_THRESHOLD and apy_diff > 10.0:
                        target_amount = single_shot_amount

                    reason = f"超級機會 ({top1['apy']:.2f}%)" if top1['apy'] > config.BIG_SHOT_THRESHOLD else f"收益顯著更高 (高出 {apy_diff:.1f}%)"
                    p(f"\n   🔥 發現{reason} {top1['symbol']}！")
                    
                    # 智能平倉邏輯: 只平倉表現不佳的倉位
                    positions_to_close = []
                    if active_positions:
                        for pos in active_positions:
                            # 只平倉那些收益顯著低於新機會的倉位 (差異 > 10%)
                            if top1['apy'] - pos.get('apy', 0) > swap_threshold:
                                positions_to_close.append(pos)

                    if positions_to_close:
                        symbols_str = ", ".join([p['symbol'] for p in positions_to_close])
                        p(f"   🚨 偵測到{reason}！正在平倉表現不佳的倉位 ({symbols_str})，以便為新機會騰出資金...")
                        
                        for pos in positions_to_close:
                            close_position(pos['symbol'], pos['amount'], api_key, secret_key, spot_info)
                        
                        # 平倉後，重新獲取最新的活躍倉位列表和可用資金
                        active_positions = get_all_futures_positions(api_key, secret_key) # Refresh active_positions after closing
                        important_event_occurred = True
                        
                        # [修正] 平倉後重新檢查資金，並根據可用資金調整目標下單金額
                        # 防止平倉釋放的資金少於預設的 target_amount 導致下單失敗
                        curr_spot_check, curr_fut_check = get_balances(api_key, secret_key)
                        total_avail_check = curr_spot_check + curr_fut_check
                        # 計算最大可下單金額 (單邊)，預留 2% 總緩衝 (雙邊各 1%)
                        max_affordable = total_avail_check / 2.03
                        
                        if target_amount > max_affordable:
                            if max_affordable >= config.BINANCE_MIN_ORDER_USDT:
                                p(f"   ⚠️ 資金重算: 平倉後可用餘額 (${total_avail_check:.1f}) 不足原目標 (${target_amount:.1f})，自動調整為 (${max_affordable:.1f})...")
                                target_amount = max_affordable
                    elif active_positions:
                        p(f"   ℹ️ 現有倉位表現尚可 (與新機會差異 < {swap_threshold:.1f}%)，保留倉位不進行換倉。")
                    
                    # 執行狙擊
                    p(f"   準備狙擊 ${target_amount:.1f}...")
                    # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                    funds_ok, _, _ = check_and_balance_funds(api_key, secret_key, target_amount, target_amount, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok:
                        execute_hedge_safe(top1['symbol'], target_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        important_event_occurred = True
                    else:
                        # Fallback 機制: 如果是大倉位失敗，嘗試降級為普通倉位
                        if target_amount == big_shot_amount:
                            p(f"   ⚠️ 資金不足以進行超級狙擊 (${target_amount:.1f})，嘗試降級為普通狙擊 (${single_shot_amount:.1f})...")
                            target_amount = single_shot_amount
                            funds_ok_retry, _, _ = check_and_balance_funds(api_key, secret_key, target_amount, target_amount, config.TRANSFER_MIN_AMOUNT)
                            if funds_ok_retry:
                                execute_hedge_safe(top1['symbol'], target_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                                important_event_occurred = True
                            else:
                                p(f"   ❌ 狙擊 {top1['symbol']} 失敗: 資金不足 (降級後仍不足)，取消狙擊。")
                                important_event_occurred = True
                        else:
                            p(f"   ❌ 狙擊 {top1['symbol']} 失敗: 資金不足，取消狙擊。")
                            important_event_occurred = True

                
                # 雙重機會 (兩個新的 10-20% 機會)
                elif top1 and top2 and not is_capital_maxed and (config.DOUBLE_TAP_MIN <= top1['apy'] <= config.DOUBLE_TAP_MAX) and (config.DOUBLE_TAP_MIN <= top2['apy'] <= config.DOUBLE_TAP_MAX):
                    p(f"\n   🔫 發現雙重機會 {top1['symbol']} ({top1['apy']:.2f}%) & {top2['symbol']} ({top2['apy']:.2f}%)！準備雙重狙擊各 ${double_tap_amount:.1f}...")
                    # 兩個 $50 總共需要 $100 現貨, $100 合約
                    # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                    funds_ok_double_tap, _, _ = check_and_balance_funds(api_key, secret_key, double_tap_amount * 2, double_tap_amount * 2, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok_double_tap:
                        execute_hedge_safe(top1['symbol'], double_tap_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        execute_hedge_safe(top2['symbol'], double_tap_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        important_event_occurred = True
                    else:
                        p(f"   ❌ 雙重狙擊 {top1['symbol']} & {top2['symbol']} 失敗: 總資金不足以進行雙重狙擊。")
                        important_event_occurred = True
                        
                        p("   🔄 嘗試單獨狙擊其中一個優質標的 (智能降級)...")
                        
                        # 從 top1 和 top2 中選出更優的一個來單獨狙擊 (APY 優先，Vol 次之)
                        opportunity_to_take_single = top1
                        if top2['apy'] > top1['apy']:
                            opportunity_to_take_single = top2
                        elif top2['apy'] == top1['apy'] and top2['vol'] > top1['vol']:
                            opportunity_to_take_single = top2

                        # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                        funds_ok_single_tap, _, _ = check_and_balance_funds(api_key, secret_key, single_shot_amount, single_shot_amount, config.TRANSFER_MIN_AMOUNT)
                        if funds_ok_single_tap:
                            p(f"   🎯 資金足夠單獨狙擊 {opportunity_to_take_single['symbol']} (${single_shot_amount:.1f})。")
                            execute_hedge_safe(opportunity_to_take_single['symbol'], single_shot_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                            important_event_occurred = True
                        else:
                            p(f"   ❌ 單獨狙擊 {opportunity_to_take_single['symbol']} 失敗: 資金不足以進行任何狙擊。")
                            important_event_occurred = True
                
                # 單獨機會 (一個新的 10-20% 機會)
                elif top1 and not is_capital_maxed and (config.DOUBLE_TAP_MIN <= top1['apy'] <= config.DOUBLE_TAP_MAX):
                    p(f"\n   🎯 發現單獨機會 {top1['symbol']} ({top1['apy']:.2f}%)！準備單發狙擊 ${single_shot_amount:.1f}...")
                    # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                    funds_ok, _, _ = check_and_balance_funds(api_key, secret_key, single_shot_amount, single_shot_amount, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok:
                        execute_hedge_safe(top1['symbol'], single_shot_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        important_event_occurred = True
                    else:
                        p(f"   ❌ 單獨狙擊 {top1['symbol']} 失敗: 資金不足，取消狙擊。")
                        important_event_occurred = True
                else: 
                    p("\n   💤 未觸發狙擊條件。")
        else:
            p("狙擊模式已關閉。\n")
    else:
        p("目前市場沒有符合條件的標的。\n")

    p(f"========================================\n")
    
    if not important_event_occurred:
        print("ℹ️ 無重要事件發生，跳過 Telegram 通知。")
        return

    # --- Telegram Report Generation ---
    telegram_report_lines = []
    
    telegram_report_lines.append(f"*{_escape_markdown_v2('🦅 資金費率投資組合報告')}*")
    telegram_report_lines.append(f"`{_escape_markdown_v2(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}`")
    telegram_report_lines.append(_escape_markdown_v2('-' * 30))
    telegram_report_lines.append("")

    # Re-fetch data for the report to ensure it's current after any actions
    active_positions_final = get_all_futures_positions(api_key, secret_key)
    # [優化] 報表生成時也使用批量獲取 (為了數據新鮮度，這裡再抓一次是合理的，但一次抓全部比抓N次快)
    report_premium_indices = fetch_public("https://fapi.binance.com/fapi/v1/premiumIndex", silent=True)
    report_index_map = {item['symbol']: item for item in report_premium_indices} if report_premium_indices else {}
    
    telegram_report_lines.append(f"*{_escape_markdown_v2('🟢 現有對沖倉位')}*")
    if active_positions_final:
        for pos in active_positions_final:
            symbol = pos['symbol']
            premium_index = report_index_map.get(symbol)
            curr_apy = float(premium_index['lastFundingRate']) * 3 * 365 * 100 if premium_index else 0
            
            status = "✅ 正常"
            if curr_apy < 0: status = "🚨 負費率"
            elif curr_apy < config.THRESHOLD_APY: status = "⚠️ 收益過低"

            telegram_report_lines.append(f"`{_escape_markdown_v2(symbol)}`")
            telegram_report_lines.append(f" `{_escape_markdown_v2(f'├ 年化: {curr_apy:.2f}%')}`")
            telegram_report_lines.append(f" `{_escape_markdown_v2(f'└ 狀態: {status}')}`")
            telegram_report_lines.append("")
    else:
        telegram_report_lines.append(f"_{_escape_markdown_v2('(目前無任何對沖倉位)')}_")
        telegram_report_lines.append("")

    telegram_report_lines.append(f"*{_escape_markdown_v2('🔍 市場優質標的')}*")
    if better_ops:
        for op in better_ops:
            if portfolio_max_apy == float('-inf'):
                diff_str = ""
            else:
                diff = op['apy'] - portfolio_max_apy
                diff_str = f"(🔥高出 {diff:.1f}%)" if diff > 0 else f"(低於 {abs(diff):.1f}%)"
            
            telegram_report_lines.append(f"`{_escape_markdown_v2(op['symbol'])}`")

            # Fix for SyntaxError: Avoid nested f-strings and quote conflicts
            line1_text = f"└ 年化: {op['apy']:.2f}% {diff_str}"
            telegram_report_lines.append(f" `{_escape_markdown_v2(line1_text)}`")
            telegram_report_lines.append("")
    else:
        telegram_report_lines.append(f"_{_escape_markdown_v2('(目前市場沒有符合條件的標的)')}_")
        telegram_report_lines.append("")

    # [修正] 在生成報表前重新獲取最新資金，確保數值準確 (因為交易過程中資金可能已變動)
    current_spot_free_final, current_fut_free_final = get_balances(api_key, secret_key)

    # Use the initial balance for the report
    telegram_report_lines.append(f"*{_escape_markdown_v2('💰 可用資金')}*")
    telegram_report_lines.append(f" `{_escape_markdown_v2(f'├ 現貨: ${current_spot_free_final:.1f}')}`")
    telegram_report_lines.append(f" `{_escape_markdown_v2(f'└ 合約: ${current_fut_free_final:.1f}')}`")
    
    # Send the formatted report to Telegram
    send_telegram_message("\n".join(telegram_report_lines), parse_mode="MarkdownV2")

if __name__ == "__main__":
    main()
    # --- 移除 Lock File ---
    lock_file_path = os.path.join(os.path.dirname(__file__), '.monitor.lock')
    if os.path.exists(lock_file_path):
        os.remove(lock_file_path)
