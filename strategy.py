# strategy.py

import datetime
import os
import time
import threading
import json
from utils import (
    get_expiry_symbol_code,
    get_next_expiry_date,
    authenticate, get_nifty_spot_price, round_to_nearest_50, get_option_symbol,
    place_order, log_trade, should_skip_trading, is_market_open,
    fetch_market_holidays, load_state,get_strategy_entry_day,save_current_state,
    get_ltp, calculate_trailing_sl, handle_expiry_square_off, reset_if_new_day, is_order_filled,notify_trader, 
    generate_weekly_summary, shutdown_watcher)
from config import CONFIG

NIFTY_LTP = None
PRINT_LTP = True
POSITIONS = {}
PNL_LOCK = 0
BOOKED_PNL = 0
RECOVERY_PENDING = {}
COMPLETED_LEGS = []
TRADE_HISTORY = []
FORCED_EXIT_LEGS = set()

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
    now = datetime.datetime.now().time()
    target = datetime.datetime.strptime(target_time, "%H:%M:%S").time()
    if now >= target:
        print(f"⏰ It’s already past entry time ({target_time}). Continuing without waiting.")
        return
    while datetime.datetime.now().time() < target:
        time.sleep(0.05)


# Retry partial fill orders
def retry_order_fill(fyers, symbol, side, qty, order_id):
    if CONFIG["MODE"] == "TEST":
        return

    for attempt in range(5):
        print(f"⏳ Checking partial fill for {symbol}, attempt {attempt+1}/5")
        time.sleep(60)

        if is_order_filled(fyers, order_id):
            print(f"✅ Order {order_id} for {symbol} is already filled. No retry needed.")
            return

        print(f"🔁 Retrying order for {symbol} (not filled yet)")
        try:
            response = place_order(fyers, symbol, side, qty)
            log_trade("RetryOrder", symbol, response)
            print(f"Retry order placed: {response}")
            order_id = response.get("id")  # update with new order ID
        except Exception as e:
            print(f"❌ Retry failed for {symbol}: {e}")


# Recovery Leg Execution
def handle_recovery_leg(fyers, original_leg, original_symbol):
    print(f"🛠️ Starting recovery for {original_leg}.1 on {original_symbol}")
    side = "BUY" if "CE" in original_symbol else "SELL"
    opposite = "PE" if side == "BUY" else "CE"
    new_atm = round_to_nearest_50(get_nifty_spot_price(fyers))
    offset = CONFIG["ATM_OFFSET_PERCENT_RECOVERY"] / 100
    strike = round_to_nearest_50(new_atm * (1 + offset if opposite == "PE" else 1 - offset))
    new_symbol = get_option_symbol(strike, opposite)

    # Get initial price of the option
    trigger_price = get_ltp(fyers, new_symbol) or 0
    print(f"Monitoring {original_leg}.1 for {CONFIG['RECOVERY_TRADE_WAIT_POINTS']}-point drop from {trigger_price} on {new_symbol}...")

    if CONFIG["RECOVERY_TRADE_WAIT_POINTS"] == 0:
        print(f"No wait configured. Executing recovery leg {original_leg}.1 immediately.")
    else:
        print(f"Monitoring {original_leg}.1 for {CONFIG['RECOVERY_TRADE_WAIT_POINTS']} point drop from {trigger_price} on {new_symbol}...")

        while True:
            current_price = get_ltp(fyers, new_symbol) or 0

            if current_price == 0:
                print(f"Waiting for valid LTP on {new_symbol}...")
                time.sleep(10)
                continue

            if current_price <= trigger_price - CONFIG["RECOVERY_TRADE_WAIT_POINTS"]:
                print(f"{CONFIG['RECOVERY_TRADE_WAIT_POINTS']}-point drop condition met for {original_leg}.1 at option price {current_price}.")
                break

        # Skip if time is past square-off
            if datetime.datetime.now().time() >= datetime.datetime.strptime(CONFIG["SQUARE_OFF_TIME"], "%H:%M:%S").time():
                print(f"Recovery leg {original_leg}.1 skipped due to time cutoff.")
                log_trade(f"{original_leg}.1_SKIPPED", new_symbol, "8-point drop not met in time")
                return

            time.sleep(10)
        
    order = place_order(fyers, new_symbol, "SELL", CONFIG["QTY"]*2)
    log_trade(f"{original_leg}.1", new_symbol, {
                "tag": "L3" if original_leg == "L1" else "L4",
                "response": order
            })
    RECOVERY_PENDING[original_leg] = False  # ✅ Clear the flag
    save_current_state(POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS, RECOVERY_PENDING)


    ltp = fyers.quotes({"symbols": new_symbol})["d"][0]["v"].get("lp", 0)
    if ltp is None or ltp == 0:
            print(f"⚠️ Skipping {original_leg}.1 — invalid LTP: {ltp}")
            log_trade("LTP_FETCH_FAIL", new_symbol, f"LTP not found or 0: {ltp}")
            return
    

    POSITIONS[f"{original_leg}.1"] = {
                "symbol": new_symbol,
                "entry_price": ltp,
                "sl_pct": CONFIG["SL_RECOVERY_PERCENT"],
                "target_pct": CONFIG["TARGET_RECOVERY_PERCENT"]
            }

    retry_order_fill(fyers, new_symbol, "SELL", CONFIG["QTY"], order.get("id"))


# Monitor SL/Target/Trailing SL/MTM lock

def monitor_positions(fyers):
    global BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS, POSITIONS

    while True:
        for leg, data in list(POSITIONS.items()):
            try:
                ltp = get_ltp(fyers, data["symbol"])
                if ltp is None:
                    print(f"⚠️ Skipping leg {leg} due to missing LTP.")
                    continue
                entry = data['entry_price']
                sl_trigger = entry * (1 + data['sl_pct'] / 100)
                target_trigger = entry * (1 - data['target_pct'] / 100)

                trailing_steps = CONFIG['TRAILING_SL_STEPS_RECOVERY'] if ".1" in leg else CONFIG['TRAILING_SL_STEPS_INITIAL']
                sl_trigger = calculate_trailing_sl(entry, ltp, trailing_steps)



                if ltp >= sl_trigger:
                    print(f"SL hit for {leg}: LTP {ltp} >= SL {sl_trigger}")
                    exit_order = place_order(fyers, data["symbol"], "BUY", CONFIG["QTY"])
                    log_trade(f"{leg}_EXIT_ORDER", data["symbol"], exit_order)
                    log_trade(f"{leg}_SL", data['symbol'], f"Exited at {ltp}")
                    
                    pnl = (data['entry_price'] - ltp) * CONFIG['QTY']
                    BOOKED_PNL += pnl

                    POSITIONS.pop(leg)
                    COMPLETED_LEGS.append(leg)  # ✅ Add to completed always
                    if leg in ("L1", "L2"):
                        RECOVERY_PENDING[leg] = True  # ✅ Mark recovery needed

                    save_current_state(POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS,RECOVERY_PENDING)

                    # 🔁 Trigger recovery if it's L1 or L2
                    if leg in ("L1", "L2"):
                        threading.Thread(
                            target=handle_recovery_leg,
                            args=(fyers, leg, data["symbol"]),
                            daemon=True
                        ).start()


                elif ltp <= target_trigger:
                    print(f"Target hit for {leg}: LTP {ltp} <= Target {target_trigger}")
                    log_trade(f"{leg}_TARGET", data['symbol'], f"Exited at {ltp}")
                    COMPLETED_LEGS.append(leg)
                    exit_order = place_order(fyers, data["symbol"], "BUY", CONFIG["QTY"])
                    log_trade(f"{leg}_EXIT_ORDER", data["symbol"], exit_order)                  
                    pnl = (data['entry_price'] - ltp) * CONFIG['QTY']
                    BOOKED_PNL += pnl
                    recovery_symbol = data["symbol"]  # Save before pop
                    POSITIONS.pop(leg)
                    save_current_state(POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS,RECOVERY_PENDING)


            except Exception as e:
                print(f"Error in monitor_positions: {e}")

        unrealized = sum([
            (data["entry_price"] - (get_ltp(fyers, data["symbol"]) or 0))
            for data in POSITIONS.values()
        ]) * CONFIG["QTY"]
        mtm = BOOKED_PNL + unrealized
        if mtm >= CONFIG['MTM_LOCK_BASE']:
            steps = int((mtm - CONFIG['MTM_LOCK_BASE']) // CONFIG['MTM_LOCK_INCREMENT'])
            PNL_LOCK = steps * CONFIG['MTM_LOCK_INCREMENT']
        if PNL_LOCK > 0 and mtm < PNL_LOCK:
            print("MTM fell below lock level. Exiting all positions.")
            if datetime.date.today() == get_next_expiry_date():
                handle_expiry_square_off(fyers, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS)

            POSITIONS.clear()
            break

        if not POSITIONS:
    # 🔁 If recovery is still pending, do NOT exit yet
            if any(RECOVERY_PENDING.get(leg) for leg in ["L1", "L2"]):
                print("⏳ Recovery trades are still pending... Waiting.")
                time.sleep(5)
                continue

            closed_legs = set(COMPLETED_LEGS)
            valid_combinations = [
                {"L1", "L2"},
                {"L1", "L2", "L1.1"},
                {"L1", "L2", "L2.1"},
                {"L1", "L2", "L1.1", "L2.1"}
            ]

            for combo in valid_combinations:
                if combo.issubset(closed_legs):
                    final_msg = f"✅ Trading is done for the week. Booked PnL = ₹{BOOKED_PNL:.2f}"
                    print(final_msg)
                    log_trade("WEEK_DONE", "ALL", final_msg)
                    summary = generate_weekly_summary(
                        booked_pnl=BOOKED_PNL,
                        trade_history=TRADE_HISTORY,
                        completed_legs=COMPLETED_LEGS,
                        recovery_pending=RECOVERY_PENDING)
                    notify_trader("📊 Weekly Trading Summary", summary)

                    save_current_state(POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS, RECOVERY_PENDING)
                    os._exit(0)


        time.sleep(5)


def heartbeat():
    while True:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 🪀 Bot alive and monitoring...")
        time.sleep(3600)  # log every hour

# Main Strategy Execution
def execute_strategy():
    print("🧠 Running execute_strategy() now...")
    global POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS
    fyers = authenticate()
    
    # Load previously saved bot state
    saved_state = load_state()
    POSITIONS = saved_state.get("POSITIONS", {})
    BOOKED_PNL = saved_state.get("BOOKED_PNL", 0)
    PNL_LOCK = saved_state.get("PNL_LOCK", 0)
    TRADE_HISTORY = saved_state.get("TRADE_HISTORY", [])
    COMPLETED_LEGS = saved_state.get("COMPLETED_LEGS", [])
    RECOVERY_PENDING = saved_state.get("RECOVERY_PENDING",{})

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=shutdown_watcher, daemon=True).start()

    for leg, pending in RECOVERY_PENDING.items():
        if pending and leg in ("L1", "L2"):
            print(f"🔁 Resuming pending recovery for {leg}.1...")
            threading.Thread(
                target=handle_recovery_leg,
                args=(fyers, leg, ""),  # symbol not needed if new one is fetched
                daemon=True
            ).start()

    today = datetime.date.today()
    expiry = get_next_expiry_date()
    last_thursday = expiry - datetime.timedelta(days=expiry.weekday() - 3)  # Thursday this week

    # If today is after this week's expiry, clear the state
    reset_if_new_day(today, POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS)





# If positions already exist, just monitor them
    if should_skip_trading(TRADE_HISTORY, COMPLETED_LEGS, BOOKED_PNL,POSITIONS):
        return


    if POSITIONS:
        print("⚠️ State shows positions open but trade history is empty. Cleaning up and resetting...")
        POSITIONS.clear()
        TRADE_HISTORY.clear()
        save_current_state(POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS,RECOVERY_PENDING)


    
    today = datetime.date.today()
    holidays = fetch_market_holidays()
    if today.strftime("%Y-%m-%d") in holidays or not is_market_open(today):
        print("Market is closed today.")
        return

    threading.Thread(target=poll_nifty_price, args=(fyers,), daemon=True).start()
    # Calculate entry day: first valid trading day after last expiry
    entry_day = get_strategy_entry_day()
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

    if "L1" not in COMPLETED_LEGS:
        call_price = get_ltp(fyers, call_symbol)
        call_order = place_order(fyers, call_symbol, "SELL", CONFIG["QTY"])
        log_trade("L1", call_symbol, {"tag": "L1", "response": call_order})
        POSITIONS["L1"] = {
            "symbol": call_symbol,
            "entry_price": call_price,
            "sl_pct": CONFIG["SL_INITIAL_PERCENT"],
            "target_pct": CONFIG["TARGET_INITIAL_PERCENT"]
        }

    if "L2" not in COMPLETED_LEGS:
        put_price = get_ltp(fyers, put_symbol)
        put_order = place_order(fyers, put_symbol, "SELL", CONFIG["QTY"])
        log_trade("L2", put_symbol, {"tag": "L2", "response": put_order})
        POSITIONS["L2"] = {
            "symbol": put_symbol,
            "entry_price": put_price,
            "sl_pct": CONFIG["SL_INITIAL_PERCENT"],
            "target_pct": CONFIG["TARGET_INITIAL_PERCENT"]
        }

    # ✅ Extend trade history only for legs actually placed
    if "L1" not in COMPLETED_LEGS:
        TRADE_HISTORY.append("L1")
    if "L2" not in COMPLETED_LEGS:
        TRADE_HISTORY.append("L2")

    # ✅ Save state once — cleanly — with everything
    save_current_state(POSITIONS, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS,RECOVERY_PENDING)


    # Turn off LTP printing to avoid noise
    global PRINT_LTP
    PRINT_LTP = False

    # Start monitoring trades
    threading.Thread(target=monitor_positions, args=(fyers,), daemon=True).start()

    while datetime.datetime.now().time() < datetime.datetime.strptime(CONFIG["SQUARE_OFF_TIME"], "%H:%M:%S").time():
        time.sleep(1)

    if datetime.date.today() == get_next_expiry_date():
        handle_expiry_square_off(fyers, BOOKED_PNL, PNL_LOCK, TRADE_HISTORY, COMPLETED_LEGS, POSITIONS, FORCED_EXIT_LEGS)


if __name__ == "__main__":
    execute_strategy()
    # Keep script alive after execution
    while True:
        time.sleep(5)
