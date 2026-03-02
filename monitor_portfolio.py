import os
import time
from datetime import datetime

# 導入外部配置
import config
# 導入交易執行和資金管理模組
from trade_executor import (
    get_keys, fetch_public, signed_request, get_exchange_info, 
    check_spot_pair_exists, get_balances, check_and_balance_funds, 
    get_all_futures_positions, close_position, execute_hedge_safe, 
    log_trade_event, send_error_notification, scan_top_opportunities, 
    close_all_active_positions
)

def main():
    api_key, secret_key = get_keys()
    if not api_key: # get_keys 內部會發送通知
        return

    # 獲取交易所資訊 (一次性，供後續精度和交易對檢查使用)
    spot_info, fut_info = get_exchange_info()
    if not spot_info or not fut_info:
        send_error_notification("無法取得交易所資訊，請檢查網路或幣安API狀態！")
        return
    
    print(f"===== 🦅 資金費率投資組合報告 =====")
    print(f"🕒 時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"========================================\n")

    # 1. 持倉監控
    active_positions = get_all_futures_positions(api_key, secret_key)
    portfolio_max_apy = 0.0
    active_symbols = []

    if active_positions:
        print("🟢 現有對沖倉位績效:")
        for pos in active_positions:
            symbol = pos['symbol']
            active_symbols.append(symbol)
            premium_index = fetch_public(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")
            if isinstance(premium_index, list): premium_index = premium_index[0]
            curr_apy = float(premium_index['lastFundingRate']) * 3 * 365 * 100 if premium_index else 0
            if curr_apy > portfolio_max_apy: portfolio_max_apy = curr_apy
            
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
                print(f"   - {symbol}: 最新入帳 {last_income_str} | 年化 {curr_apy:.2f}% | 狀態: {status}")
                # 更新 close_position 調用，傳遞 spot_info
                close_position(symbol, pos['amount'], api_key, secret_key, spot_info)
                continue
            elif curr_apy < config.THRESHOLD_APY: status = "⚠️ 警告: 收益過低"
            
            print(f"   - {symbol}: 最新入帳 {last_income_str} | 年化 {curr_apy:.2f}% | 狀態: {status}")
        print(f"----------------------------------------")
    else:
        print("🟢 (目前無任何對沖倉位)\n")
        # 優化點 3.3: 調整 portfolio_max_apy 初始化與 diff_str 顯示
        portfolio_max_apy = float('-inf') # 如果沒有持倉，則視為所有機會都高於當前收益

    # 2. 掃描與狙擊
    print(f"🔍 市場前 5 名優質標的 (24h成交量 > ${config.MIN_VOLUME_USDT/1_000_000:.0f}M)...\n")
    better_ops = scan_top_opportunities(spot_info)
    
    if better_ops:
        for op in better_ops:
            diff = op['apy'] - portfolio_max_apy
            diff_str = f" (🔥 高出 {diff:.1f}%)" if diff > 0 else (f" (低於當前 {abs(diff):.1f}%)" if portfolio_max_apy != float('-inf') else " (無持倉對比)") # 優化點 3.3
            print(f"{op['symbol']:<12} {op['apy']:.2f}%      ${op['vol']/1_000_000:.0f}M {diff_str}")
        
        # 狙擊邏輯
        if config.SNIPER_MODE:
            # 優化: 先過濾掉已持有的倉位，只關注"可下單"的機會
            # 這樣即使持有市場第一名，也不會因此錯過市場第二名的機會
            available_ops = [op for op in better_ops if op['symbol'] not in active_symbols]
            
            top1 = available_ops[0] if available_ops else None
            top2 = available_ops[1] if len(available_ops) > 1 else None

            # 優化點 3.2: 將 get_balances 移入 check_and_balance_funds 內部，並使用其返回的最新餘額
            # 重新獲取當前資金，因為資金平衡操作會改變餘額
            # spot_free, fut_free = get_balances(api_key, secret_key) # 此行已被 check_and_balance_funds 內部處理
            current_spot_free_initial, current_fut_free_initial = get_balances(api_key, secret_key) # 用於顯示初始資金
            print(f"   (當前可用資金 Spot:${current_spot_free_initial:.1f} Fut:${current_fut_free_initial:.1f})")

            # --- 新增邏輯: 已有 $50 倉位，且市場出現雙重機會，則只補一個 $50 --- 
            # 判斷是否只持有一個 $50 倉位 
            has_one_50_position = len(active_positions) == 1 and active_positions[0]['amount'] == config.SINGLE_SHOT_AMOUNT_10_20 
            
            if has_one_50_position:
                held_symbol = active_positions[0]['symbol']
                # 從可用機會中選出最好的那個 (排除已持有的)
                opportunity_to_take = None
                # 直接使用已經過濾好的 available_ops
                if top1 and config.DOUBLE_TAP_MIN <= top1['apy'] <= config.DOUBLE_TAP_MAX:
                    opportunity_to_take = top1

                if opportunity_to_take:
                    print(f"\n   🎯 已持有一倉位 ({held_symbol})，市場出現優質新機會 {opportunity_to_take['symbol']} ({opportunity_to_take['apy']:.2f}%)！準備補齊第二個倉位，單發狙擊 ${config.SINGLE_SHOT_AMOUNT_10_20:.1f}...")
                    funds_ok, _, _ = check_and_balance_funds(api_key, secret_key, config.SINGLE_SHOT_AMOUNT_10_20, config.SINGLE_SHOT_AMOUNT_10_20, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok: # 如果資金平衡成功
                        execute_hedge_safe(opportunity_to_take['symbol'], config.SINGLE_SHOT_AMOUNT_10_20, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                    else:
                        error_msg = f"狙擊 {opportunity_to_take['symbol']} 失敗: 資金不足，取消狙擊。"
                        print(f"   ❌ {error_msg}")
                        send_error_notification(error_msg)
                else:
                    print("\n   💤 最佳新機會收益未達單獨狙擊門檻，或無新機會。")
            
            # --- 其他情況：無持倉，或多個持倉，或未滿足上述特殊條件 --- 
            else: 
                # 計算收益差異 (僅在有持倉時計算)
                apy_diff = 0.0
                if top1 and active_positions:
                    apy_diff = top1['apy'] - portfolio_max_apy

                # 超級機會 (>20%) 或 顯著優於當前持倉 (>10% 差異)
                if top1 and (top1['apy'] > config.BIG_SHOT_THRESHOLD or apy_diff > 10.0):
                    # 決定目標金額: 
                    # 1. 如果是真正的超級機會 (>20%)，嘗試用大倉位 (Big Shot)
                    # 2. 如果只是因為差異大而換倉 (例如 5% -> 16%)，但未達超級門檻，則保守使用普通倉位，避免小帳戶平倉後不夠錢開大倉
                    target_amount = config.BIG_SHOT_AMOUNT
                    if top1['apy'] <= config.BIG_SHOT_THRESHOLD and apy_diff > 10.0:
                        target_amount = config.SINGLE_SHOT_AMOUNT_10_20

                    reason = f"超級機會 ({top1['apy']:.2f}%)" if top1['apy'] > config.BIG_SHOT_THRESHOLD else f"收益顯著更高 (高出 {apy_diff:.1f}%)"
                    print(f"\n   🔥 發現{reason} {top1['symbol']}！")
                    
                    # 新增邏輯: 平倉所有現有倉位
                    if active_positions: # 只有在有活躍倉位時才平倉
                        print(f"   🚨 偵測到{reason}！正在平倉所有現有對沖倉位，以便為新機會騰出資金...")
                        close_all_active_positions(api_key, secret_key, spot_info) # 傳遞 spot_info
                        # 平倉後，重新獲取最新的活躍倉位列表和可用資金
                        active_positions = get_all_futures_positions(api_key, secret_key) # Refresh active_positions after closing
                    
                    # 執行狙擊
                    print(f"   準備狙擊 ${target_amount:.1f}...")
                    # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                    funds_ok, _, _ = check_and_balance_funds(api_key, secret_key, target_amount, target_amount, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok:
                        execute_hedge_safe(top1['symbol'], target_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                    else:
                        # Fallback 機制: 如果是大倉位失敗，嘗試降級為普通倉位
                        if target_amount == config.BIG_SHOT_AMOUNT:
                            print(f"   ⚠️ 資金不足以進行超級狙擊 (${target_amount})，嘗試降級為普通狙擊 (${config.SINGLE_SHOT_AMOUNT_10_20})...")
                            target_amount = config.SINGLE_SHOT_AMOUNT_10_20
                            funds_ok_retry, _, _ = check_and_balance_funds(api_key, secret_key, target_amount, target_amount, config.TRANSFER_MIN_AMOUNT)
                            if funds_ok_retry:
                                execute_hedge_safe(top1['symbol'], target_amount, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                            else:
                                error_msg = f"狙擊 {top1['symbol']} 失敗: 資金不足 (降級後仍不足)，取消狙擊。"
                                print(f"   ❌ {error_msg}")
                                send_error_notification(error_msg)
                        else:
                            error_msg = f"狙擊 {top1['symbol']} 失敗: 資金不足，取消狙擊。"
                            print(f"   ❌ {error_msg}")
                            send_error_notification(error_msg)

                
                # 雙重機會 (兩個新的 10-20% 機會)
                elif top1 and top2 and (config.DOUBLE_TAP_MIN <= top1['apy'] <= config.DOUBLE_TAP_MAX) and (config.DOUBLE_TAP_MIN <= top2['apy'] <= config.DOUBLE_TAP_MAX):
                    print(f"\n   🔫 發現雙重機會 {top1['symbol']} ({top1['apy']:.2f}%) & {top2['symbol']} ({top2['apy']:.2f}%)！準備雙重狙擊各 ${config.DOUBLE_TAP_AMOUNT_EACH:.1f}...")
                    # 兩個 $50 總共需要 $100 現貨, $100 合約
                    # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                    funds_ok_double_tap, _, _ = check_and_balance_funds(api_key, secret_key, config.DOUBLE_TAP_AMOUNT_EACH * 2, config.DOUBLE_TAP_AMOUNT_EACH * 2, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok_double_tap:
                        execute_hedge_safe(top1['symbol'], config.DOUBLE_TAP_AMOUNT_EACH, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        execute_hedge_safe(top2['symbol'], config.DOUBLE_TAP_AMOUNT_EACH, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                    else:
                        error_msg = f"雙重狙擊 {top1['symbol']} & {top2['symbol']} 失敗: 總資金不足以進行雙重狙擊。"
                        print(f"   ❌ {error_msg}")
                        send_error_notification(error_msg)
                        
                        print("   🔄 嘗試單獨狙擊其中一個優質標的 (智能降級)...")
                        
                        # 從 top1 和 top2 中選出更優的一個來單獨狙擊 (APY 優先，Vol 次之)
                        opportunity_to_take_single = top1
                        if top2['apy'] > top1['apy']:
                            opportunity_to_take_single = top2
                        elif top2['apy'] == top1['apy'] and top2['vol'] > top1['vol']:
                            opportunity_to_take_single = top2

                        # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                        funds_ok_single_tap, _, _ = check_and_balance_funds(api_key, secret_key, config.SINGLE_SHOT_AMOUNT_10_20, config.SINGLE_SHOT_AMOUNT_10_20, config.TRANSFER_MIN_AMOUNT)
                        if funds_ok_single_tap:
                            print(f"   🎯 資金足夠單獨狙擊 {opportunity_to_take_single['symbol']} (${config.SINGLE_SHOT_AMOUNT_10_20:.1f})。")
                            execute_hedge_safe(opportunity_to_take_single['symbol'], config.SINGLE_SHOT_AMOUNT_10_20, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                        else:
                            error_msg = f"單獨狙擊 {opportunity_to_take_single['symbol']} 失敗: 資金不足以進行任何狙擊。"
                            print(f"   ❌ {error_msg}")
                            send_error_notification(error_msg)
                
                # 單獨機會 (一個新的 10-20% 機會)
                elif top1 and (config.DOUBLE_TAP_MIN <= top1['apy'] <= config.DOUBLE_TAP_MAX):
                    print(f"\n   🎯 發現單獨機會 {top1['symbol']} ({top1['apy']:.2f}%)！準備單發狙擊 ${config.SINGLE_SHOT_AMOUNT_10_20:.1f}...")
                    # 優化點 3.2: 確保資金平衡後使用正確的餘額，避免重複獲取
                    funds_ok, _, _ = check_and_balance_funds(api_key, secret_key, config.SINGLE_SHOT_AMOUNT_10_20, config.SINGLE_SHOT_AMOUNT_10_20, config.TRANSFER_MIN_AMOUNT)
                    if funds_ok:
                        execute_hedge_safe(top1['symbol'], config.SINGLE_SHOT_AMOUNT_10_20, api_key, secret_key, spot_info, fut_info, config.BINANCE_MIN_ORDER_USDT)
                    else:
                        error_msg = f"單獨狙擊 {top1['symbol']} 失敗: 資金不足，取消狙擊。"
                        print(f"   ❌ {error_msg}")
                        send_error_notification(error_msg)
                else: 
                    print("\n   💤 未觸發狙擊條件。")
        else:
            print("狙擊模式已關閉。\n")
    else:
        print("目前市場沒有符合條件的標的。\n")

    print(f"========================================\n")

if __name__ == "__main__":
    main()
