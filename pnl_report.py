import pandas as pd
import re
import config
import urllib.request
import urllib.parse

def calculate_pnl(trades_df):
    """
    Calculates PnL for a given trading pair, specifically for hedged (spot long + futures short) positions.
    Considers spot buys, futures sells (opening short), futures buys (closing short), spot sells, and funding fees.
    """
    realized_pnl = 0
    total_funding_fees = 0
    processed_funding_ids = set()
    
    # Temporary storage for open spot positions to match with closing sells (FIFO)
    open_spot_positions = [] # list of {'quantity': float, 'price': float, 'fee': float}
    # Temporary storage for open futures short positions to match with closing buys
    open_futures_short_positions = [] # list of {'quantity': float, 'price': float, 'fee': float}

    # Sort by timestamp to ensure FIFO logic is correctly applied
    trades_df = trades_df.sort_values(by='Timestamp').reset_index(drop=True)

    for index, row in trades_df.iterrows():
        if row['EventType'] == 'Open_Spot':
            open_spot_positions.append({
                'quantity': row['Quantity'],
                'price': row['Price'],
                'fee': row['Fee']
            })
        elif row['EventType'] == 'Close_Spot':
            amount_to_sell = row['Quantity']
            sell_price = row['Price']
            sell_fee = row['Fee']

            while amount_to_sell > 0 and open_spot_positions:
                oldest_spot_pos = open_spot_positions[0]
                amount_from_oldest = min(amount_to_sell, oldest_spot_pos['quantity'])

                # PnL from spot trade
                realized_pnl += (sell_price - oldest_spot_pos['price']) * amount_from_oldest
                realized_pnl -= (oldest_spot_pos['fee'] * (amount_from_oldest / oldest_spot_pos['quantity']) if oldest_spot_pos['quantity'] > 0 else 0)
                realized_pnl -= (sell_fee * (amount_from_oldest / row['Quantity']) if row['Quantity'] > 0 else 0)

                oldest_spot_pos['quantity'] -= amount_from_oldest
                amount_to_sell -= amount_from_oldest

                if oldest_spot_pos['quantity'] == 0:
                    open_spot_positions.pop(0)

            if amount_to_sell > 0:
                print(f"Warning: Attempted to sell more spot than open buy positions for {row['Symbol']}. Remaining to sell: {amount_to_sell}")

        elif row['EventType'] == 'Open_Futures': # This is a short position
            open_futures_short_positions.append({
                'quantity': row['Quantity'],
                'price': row['Price'], # This is the price at which we opened the short
                'fee': row['Fee']
            })
        elif row['EventType'] == 'Close_Futures': # This is buying to close a short
            amount_to_buy = row['Quantity']
            buy_price = row['Price'] # This is the price at which we close the short
            buy_fee = row['Fee']
            
            while amount_to_buy > 0 and open_futures_short_positions:
                oldest_fut_short_pos = open_futures_short_positions[0]
                amount_from_oldest = min(amount_to_buy, oldest_fut_short_pos['quantity'])

                # PnL from futures short trade: (short_open_price - short_close_price) * amount
                realized_pnl += (oldest_fut_short_pos['price'] - buy_price) * amount_from_oldest
                realized_pnl -= (oldest_fut_short_pos['fee'] * (amount_from_oldest / oldest_fut_short_pos['quantity']) if oldest_fut_short_pos['quantity'] > 0 else 0)
                realized_pnl -= (buy_fee * (amount_from_oldest / row['Quantity']) if row['Quantity'] > 0 else 0)

                oldest_fut_short_pos['quantity'] -= amount_from_oldest
                amount_to_buy -= amount_from_oldest

                if oldest_fut_short_pos['quantity'] == 0:
                    open_futures_short_positions.pop(0)
            
            if amount_to_buy > 0:
                print(f"Warning: Attempted to buy more futures than open short positions for {row['Symbol']}. Remaining to buy: {amount_to_buy}")

        elif row['EventType'] == 'Funding_Fee':
            # 解析 Message 中的 ID 進行去重 (格式: "APY:xx%, ID:12345")
            message = str(row['Message'])
            match = re.search(r'ID:(\d+)', message)
            if match:
                tran_id = match.group(1)
                if tran_id in processed_funding_ids:
                    continue # 跳過重複的資金費記錄
                processed_funding_ids.add(tran_id)
            
            total_funding_fees += row['FundingFee']
        
        # Rollback_Spot is essentially a Close_Spot for the purposes of PnL calculation
        elif row['EventType'] == 'Rollback_Spot':
            amount_to_sell = row['Quantity']
            sell_price = row['Price']
            sell_fee = row['Fee']

            while amount_to_sell > 0 and open_spot_positions:
                oldest_spot_pos = open_spot_positions[0]
                amount_from_oldest = min(amount_to_sell, oldest_spot_pos['quantity'])

                # PnL from spot trade
                realized_pnl += (sell_price - oldest_spot_pos['price']) * amount_from_oldest
                realized_pnl -= (oldest_spot_pos['fee'] * (amount_from_oldest / oldest_spot_pos['quantity']) if oldest_spot_pos['quantity'] > 0 else 0)
                realized_pnl -= (sell_fee * (amount_from_oldest / row['Quantity']) if row['Quantity'] > 0 else 0)

                oldest_spot_pos['quantity'] -= amount_from_oldest
                amount_to_sell -= amount_from_oldest

                if oldest_spot_pos['quantity'] == 0:
                    open_spot_positions.pop(0)

            if amount_to_sell > 0:
                print(f"Warning: Attempted to sell more spot than open buy positions during rollback for {row['Symbol']}. Remaining to sell: {amount_to_sell}")


    # Note: This PnL calculation assumes that for a hedged strategy, spot buy and futures short are matched.
    # It tracks PnL from the closing of these positions.
    
    return realized_pnl, total_funding_fees

def send_telegram_report(message):
    telegram_user_id = config.TELEGRAM_USER_ID
    telegram_bot_token = config.TELEGRAM_BOT_TOKEN
    
    if not telegram_user_id or not telegram_bot_token:
        print("⚠️ Telegram 通知未配置 (TELEGRAM_USER_ID 或 TELEGRAM_BOT_TOKEN 未設定)，無法發送報表。")
        return

    print(f"📤 正在發送報表至 Telegram...")
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": telegram_user_id, "text": message}).encode('utf-8')
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req) as response:
            print(f"✅ Telegram 通知發送成功！")
    except Exception as e:
        print(f"❌ 無法發送 Telegram 通知: {e}")

def main():
    try:
        df = pd.read_csv(config.TRADE_LOG_FILE)
    except FileNotFoundError:
        print(f"Error: The trade log file '{config.TRADE_LOG_FILE}' was not found.")
        print("Please ensure 'trade_log.csv' exists in the same directory as 'config.py'.")
        return

    # Convert timestamp to datetime objects and sort
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values(by='Timestamp').reset_index(drop=True)

    # Initialize overall PnL
    total_realized_pnl = 0
    total_funding_fees = 0

    pnl_by_pair = {}

    # Group by Symbol and calculate PnL for each
    for symbol, group in df.groupby('Symbol'):
        realized_pnl_pair, funding_fees_pair = calculate_pnl(group)
        pnl_by_pair[symbol] = {
            'realized_pnl': realized_pnl_pair,
            'funding_fees': funding_fees_pair,
            'net_pnl': realized_pnl_pair + funding_fees_pair
        }
        total_realized_pnl += realized_pnl_pair
        total_funding_fees += funding_fees_pair

    # 建立報表內容字串
    report_lines = []
    def p(text=""):
        print(text)
        report_lines.append(text)

    # Generate Report
    p("--- 🦅 資金費率 PnL 報表 ---")
    p("")

    if not pnl_by_pair:
        p("No trading pairs found to analyze.")
    else:
        for symbol, pnl_data in pnl_by_pair.items():
            p(f"Pair: {symbol}")
            p(f"  Realized PnL: {pnl_data['realized_pnl']:.2f}")
            p(f"  Funding Fees: {pnl_data['funding_fees']:.2f}")
            p(f"  Net PnL: {pnl_data['net_pnl']:.2f}")
            p("-" * 20)
        
        p("")
        p(f"Total Realized PnL: {total_realized_pnl:.2f}")
        p(f"Total Funding Fees: {total_funding_fees:.2f}")
        p(f"Overall Net PnL: {total_realized_pnl + total_funding_fees:.2f}")

    send_telegram_report("\n".join(report_lines))

    print("\n")
    print("--- How to Run This Script ---")
    print('''1. Save this content as `pnl_report.py`.
2. Ensure you have a `config.py` file in the same directory, defining `TRADE_LOG_FILE` (e.g., `TRADE_LOG_FILE = "trade_log.csv"`).
3. Ensure your trade data is in a CSV file named `trade_log.csv` (or whatever `TRADE_LOG_FILE` is set to).
   The CSV should have columns: `Timestamp`, `EventType`, `Symbol`, `Side`, `USDTValue`, `Quantity`, `Price`, `Fee`, `FundingFee`, `Message`.
4. Install pandas: `pip install pandas`
5. Run the script from your terminal: `python3 pnl_report.py` (or `python pnl_report.py` if `python` is aliased to `python3`)
The report will be printed to your console.''')


if __name__ == "__main__":
    main()
