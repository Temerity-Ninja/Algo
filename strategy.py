# strategy.py

import datetime
import time
import threading
from utils import (
    get_expiry_symbol_code,
    get_next_expiry_date,
    authenticate, get_nifty_spot_price, round_to_nearest_50, get_option_symbol,
    place_order, log_trade, square_off_all_positions, is_market_open,
    fetch_market_holidays
)
from config import CONFIG

NIFTY_LTP = None
PRINT_LTP = True
POSITIONS = {}
PNL_LOCK = 0
BOOKED_PNL = 0
RECOVERY_PENDING = {}

# Fetch Nifty price continuously every second
def poll_nifty_price(fyers):
    global NIFTY_LTP, PRINT_LTP
    while True:
        try:
            start = time.time()
            NIFTY_LTP = get_nifty_spot_price(fyers)
            if PRINT_LTP:
                print(f"NIFTY LTP: {NIFTY_LTP} (fetched in {(time.time()-start)*1000:.2f} ms)")
        except Exception as e:
            print(f"Error fetching Nifty LTP: {e}")
        time.sleep(10)

# Wait for specified time
def wait_until(target_time):
     while datetime.datetime.now().time() < datetime.datetime.strptime(target_time, "%H:%M:%S").time():
        time.sleep(0.5)

# Retry partial fill orders
def retry_order_fill(fyers, symbol, side, qty):
    for attempt in range(5):
        print(f"Checking partial fill for {symbol}, attempt {attempt+1}/5")
        time.sleep(60)  # wait 1 minute
        try:
            response = place_order(fyers, symbol, side, qty)
            log_trade("RetryOrder", symbol, response)
            print(f"Retry order placed for {symbol}: {response}")
            break
        except Exception as e:
            print(f"Retry attempt {attempt+1} failed for {symbol}: {e}")

# Recovery Leg Execution
def handle_recovery_leg(fyers, original_leg):
    side = "BUY" if "CE" in POSITIONS[original_leg]["symbol"] else "SELL"
    opposite = "PE" if side == "BUY" else "CE"
    new_atm = round_to_nearest_50(get_nifty_spot_price(fyers))
    offset = CONFIG["ATM_OFFSET_PERCENT_RECOVERY"] / 100
    strike = round_to_nearest_50(new_atm * (1 + offset if opposite == "PE" else 1 - offset))
    new_symbol = get_option_symbol(strike, opposite)

    trigger_price = get_nifty_spot_price(fyers)
    print(f"Monitoring {original_leg}.1 for 8-point drop from {trigger_price}...")
    while True:
        current_price = get_nifty_spot_price(fyers)
        if current_price <= trigger_price - CONFIG["RECOVERY_TRADE_WAIT_POINTS"]:
            print(f"8-point drop condition met for {original_leg}.1 at price {current_price}.")
            break
        elif datetime.datetime.now().time() >= datetime.datetime.strptime(CONFIG["SQUARE_OFF_TIME"], "%H:%M:%S").time():
            print(f"Recovery leg {original_leg}.1 skipped due to time cutoff.")
            log_trade(f"{original_leg}.1_SKIPPED", new_symbol, "8-point drop condition not met in time")
        time.sleep(60)
        time.sleep(2)
        time.sleep(2)

    order = place_order(fyers, new_symbol, "SELL", CONFIG["QTY"] * 2)
    log_trade(f"{original_leg}.1", new_symbol, {"tag": "L3" if original_leg == "L1" else "L4", "response": order})
    POSITIONS[f"{original_leg}.1"] = {
        "symbol": new_symbol,
        "entry_price": get_nifty_spot_price(fyers),
        "sl_pct": CONFIG["SL_RECOVERY_PERCENT"],
        "target_pct": CONFIG["TARGET_RECOVERY_PERCENT"]
    }

    # Retry logic in case of partial fill handling
    retry_order_fill(fyers, new_symbol, "SELL", CONFIG["QTY"] * 2)

# Monitor SL/Target/Trailing SL/MTM lock

def monitor_positions(fyers):
    global PNL_LOCK
    while True:
        for leg, data in list(POSITIONS.items()):
            try:
                response = fyers.quotes({"symbols": data['symbol']})
                ltp = response["d"][0]["v"]["lp"]
                entry = data['entry_price']
                sl_trigger = entry * (1 + data['sl_pct'] / 100)
                target_trigger = entry * (1 - data['target_pct'] / 100)

                for threshold, new_sl_pct in CONFIG['TRAILING_SL_STEPS_INITIAL']:
                    if ltp < entry * (threshold / 100):
                        sl_trigger = entry * (1 + new_sl_pct / 100)

                if ltp >= sl_trigger:
                    print(f"SL hit for {leg}: LTP {ltp} >= SL {sl_trigger}")
                    log_trade(f"{leg}_SL", data['symbol'], f"Exited at {ltp}")
                    pnl = (data['entry_price'] - ltp) * CONFIG['QTY']
                    global BOOKED_PNL
                    BOOKED_PNL += pnl
                    POSITIONS.pop(leg)
                    if leg in ["L1", "L2"]:
                        threading.Thread(target=handle_recovery_leg, args=(fyers, leg), daemon=True).start()
                elif ltp <= target_trigger:
                    print(f"Target hit for {leg}: LTP {ltp} <= Target {target_trigger}")
                    log_trade(f"{leg}_TARGET", data['symbol'], f"Exited at {ltp}")
                    pnl = (data['entry_price'] - ltp) * CONFIG['QTY']
                    BOOKED_PNL += pnl
                    POSITIONS.pop(leg)
            except Exception as e:
                print(f"Error in monitor_positions: {e}")

        unrealized = sum([
            (data['entry_price'] - fyers.quotes({"symbols": data['symbol']})["d"][0]["v"]["lp"])
            for data in POSITIONS.values()
        ]) * CONFIG['QTY']
        mtm = BOOKED_PNL + unrealized
        if mtm >= CONFIG['MTM_LOCK_BASE']:
            steps = int((mtm - CONFIG['MTM_LOCK_BASE']) // CONFIG['MTM_LOCK_INCREMENT'])
            PNL_LOCK = steps * CONFIG['MTM_LOCK_INCREMENT']
        if PNL_LOCK > 0 and mtm < PNL_LOCK:
            print("MTM fell below lock level. Exiting all positions.")
            if datetime.date.today() == get_next_expiry_date():
                square_off_all_positions(fyers)
            POSITIONS.clear()
            break
        time.sleep(5)

# Main Strategy Execution
def heartbeat():
    while True:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 🪀 Bot alive and monitoring...")
        time.sleep(3600)  # log every hour

def execute_strategy():
    fyers = authenticate()
    threading.Thread(target=heartbeat, daemon=True).start()

    today = datetime.date.today()
    holidays = fetch_market_holidays()
    if today.strftime("%Y-%m-%d") in holidays or not is_market_open(today):
        print("Market is closed today.")
        return

    threading.Thread(target=poll_nifty_price, args=(fyers,), daemon=True).start()
    # Calculate entry day: first valid trading day after last expiry
    expiry_day = get_next_expiry_date()
    entry_day = expiry_day - datetime.timedelta(days=3)
    if not is_market_open(entry_day) or entry_day.strftime("%Y-%m-%d") in holidays:
        entry_day += datetime.timedelta(days=1)

    if today != entry_day:
        print(f"Today is not the entry day. Entry scheduled on: {entry_day}")
        try:
            start = time.time()
            pos_data = fyers.positions()
            duration = (time.time() - start) * 1000
            total_pnl = 0
            for pos in pos_data.get("netPositions", []):
                total_pnl += pos.get("pl", 0)
            print(f"Live Account PnL (including manual trades): ₹{total_pnl:.2f} (fetched in {duration:.2f} ms)")
        except Exception as e:
            print(f"Could not fetch account PnL: {e}")
        print("Monitoring existing trades (manual or prior) for SL/Target/MTM/Exit.")
        threading.Thread(target=monitor_positions, args=(fyers,), daemon=True).start()
    if datetime.datetime.now().date() == get_next_expiry_date():
        while datetime.datetime.now().time() < datetime.datetime.strptime(CONFIG["SQUARE_OFF_TIME"], "%H:%M:%S").time():
            time.sleep(1)

        if datetime.date.today() == get_next_expiry_date():
            square_off_all_positions(fyers)
            log_trade("SquareOff", "All", "Positions squared off at expiry")
        return

    wait_until(CONFIG["ENTRY_TIME"])

    attempts = 0
    while NIFTY_LTP is None and attempts < 10:
        print("Waiting for NIFTY LTP...")
        time.sleep(1)
        attempts += 1

    spot_price = NIFTY_LTP
    if spot_price is None:
        print("Failed to fetch initial Nifty LTP. Exiting.")
        return

    atm = round_to_nearest_50(spot_price)
    call_strike = round_to_nearest_50(atm * (1 + CONFIG["ATM_OFFSET_PERCENT_INITIAL"] / 100))
    put_strike = round_to_nearest_50(atm * (1 - CONFIG["ATM_OFFSET_PERCENT_INITIAL"] / 100))

    expiry_date = get_next_expiry_date()
    while expiry_date.strftime("%Y-%m-%d") in holidays:
        expiry_date -= datetime.timedelta(days=1)
    expiry_str = get_expiry_symbol_code(expiry_date)
    call_symbol = f"NSE:NIFTY{expiry_str}{call_strike}CE"
    put_symbol = f"NSE:NIFTY{expiry_str}{put_strike}PE"

    call_order = place_order(fyers, call_symbol, "SELL", CONFIG["QTY"])
    put_order = place_order(fyers, put_symbol, "SELL", CONFIG["QTY"])

    log_trade("L1", call_symbol, {"tag": "L1", "response": call_order})
    log_trade("L2", put_symbol, {"tag": "L2", "response": put_order})

    global PRINT_LTP
    PRINT_LTP = False

    POSITIONS["L1"] = {"symbol": call_symbol, "entry_price": get_nifty_spot_price(fyers), "sl_pct": CONFIG['SL_INITIAL_PERCENT'], "target_pct": CONFIG['TARGET_INITIAL_PERCENT']}
    POSITIONS["L2"] = {"symbol": put_symbol, "entry_price": get_nifty_spot_price(fyers), "sl_pct": CONFIG['SL_INITIAL_PERCENT'], "target_pct": CONFIG['TARGET_INITIAL_PERCENT']}

    threading.Thread(target=monitor_positions, args=(fyers,), daemon=True).start()

    while datetime.datetime.now().time() < datetime.datetime.strptime(CONFIG["SQUARE_OFF_TIME"], "%H:%M:%S").time():
        time.sleep(1)

    if datetime.date.today() == get_next_expiry_date():
        square_off_all_positions(fyers)
        log_trade("SquareOff", "All", "Positions squared off at expiry")

if __name__ == "__main__":
    execute_strategy()
    # Keep script alive after execution
    while True:
        time.sleep(5)
