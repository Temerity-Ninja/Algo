# utils.py

import requests
import pyotp
import base64
import smtplib
import datetime
import json
import os
import time
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel
from config import CONFIG, HOLIDAYS_URL,EMAIL_SETTINGS
from email.mime.text import MIMEText

# Authentication with FYERS
def getEncodedString(string):
    return base64.b64encode(str(string).encode("ascii")).decode("ascii")


#SENDING ALERT MAIL
def send_email(subject, body):
    if not EMAIL_SETTINGS.get("ENABLED"):
        print("📭 Email notifications are disabled.")
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_SETTINGS["SENDER_EMAIL"]
        msg["To"] = ", ".join(EMAIL_SETTINGS["RECEIVER_EMAIL"])

        with smtplib.SMTP(EMAIL_SETTINGS["SMTP_SERVER"], EMAIL_SETTINGS["SMTP_PORT"]) as server:
            server.starttls()
            server.login(EMAIL_SETTINGS["SENDER_EMAIL"], EMAIL_SETTINGS["SENDER_PASSWORD"])
            server.send_message(msg)

        print("✅ Email sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")


#Telegram Alert
def send_telegram(message):
    TOKEN = "7709231427:AAH-3g7ve-lPqLN4UQptM2AyxHvrdw11EfQ"
    CHAT_ID = "-1002653287995"  # ← your Somani Algo group chat ID

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'  # optional: use 'HTML' if preferred
    }

    try:
        r = requests.post(url, json=payload)
        if r.ok:
            print("📢 Telegram group alert sent.")
        else:
            print(f"❌ Failed to send: {r.text}")
    except Exception as e:
        print(f"Telegram error: {e}")


#Generate weekly summary
def generate_weekly_summary(booked_pnl, trade_history, completed_legs, recovery_pending, log_file=None):
    summary_lines = [
        "📈 *Weekly Strategy Summary Report*",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ *Total Booked PnL:* ₹{booked_pnl:.2f}",
        f"📅 *Week Ending:* {datetime.date.today()}",
        "",
        "*📍 Trade Summary:*"
    ]

    for leg in ["L1", "L2", "L1.1", "L2.1"]:
        if leg in completed_legs:
            status = "✅ Target Hit" if "TARGET" in str(trade_history) else "❌ SL Hit"
        elif recovery_pending.get(leg.replace(".1", ""), False):
            status = "⏳ Recovery Pending"
        else:
            status = "⏸ Not Triggered"
        summary_lines.append(f"- {leg}: {status}")

    summary_lines += [
        "",
        "*📊 Stats:*",
        f"• Total Trades Placed: {len(trade_history)}",
        f"• Recovery Legs Triggered: {sum(1 for k in recovery_pending if recovery_pending[k])}",
        f"• Final Exit Time: {datetime.datetime.now().strftime('%H:%M:%S')}",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "🚀 Onward to next week. Stay sharp!"
    ]

    return "\n".join(summary_lines)


#Both alert
def notify_trader(subject, body):
    # Send Email
    try:
        send_email(subject, body)
    except Exception as e:
        print(f"❌ Email alert failed: {e}")

    # Send Telegram
    try:
        send_telegram(f"*{subject}*\n\n{body}")
    except Exception as e:
        print(f"❌ Telegram alert failed: {e}")



# 🔐 Logs in to Fyers using OTP+PIN auth and returns an authenticated FyersModel object
def authenticate():
    redirect_uri = "https://127.0.0.1:5000/"
    client_id = "W6GJRU9TPF-100"
    secret_key = "IUJXDSDSG3"
    fy_id = "XA62721"
    totp_key = "W772UQH6IV3SBKM4PL6MXYWXJSPNHD5P"
    pin = "2873"

    try:
        res = requests.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
                            json={"fy_id": getEncodedString(fy_id), "app_id": "2"}).json()

        if datetime.datetime.now().second % 30 > 27:
            time.sleep(5)
        otp = pyotp.TOTP(totp_key).now()
        res2 = requests.post("https://api-t2.fyers.in/vagator/v2/verify_otp",
                             json={"request_key": res["request_key"], "otp": otp}).json()

        ses = requests.Session()
        payload2 = {"request_key": res2["request_key"], "identity_type": "pin", "identifier": getEncodedString(pin)}
        res3 = ses.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", json=payload2).json()
        ses.headers.update({'authorization': f"Bearer {res3['data']['access_token']}"})

        payload3 = {
            "fyers_id": fy_id,
            "app_id": client_id[:-4],
            "redirect_uri": redirect_uri,
            "appType": "100",
            "code_challenge": "",
            "state": "None",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        res4 = ses.post("https://api-t1.fyers.in/api/v3/token", json=payload3).json()
        if 'Url' not in res4:
            raise Exception("Could not find 'Url' in response. Login may have failed.")

        parsed = urlparse(res4['Url'])
        auth_code = parse_qs(parsed.query)['auth_code'][0]

        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )
        session.set_token(auth_code)
        response = session.generate_token()
        access_token = response['access_token']

        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)

        fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path=log_dir)

        return fyers

    except Exception as e:
        notify_trader(subject="🚫 Authentication Failed", body=f"An error occurred during login:\n\n{str(e)}")
        raise



# 📆 Fetches market holidays from NSE and adds manual ones
def fetch_market_holidays():
    holidays = CONFIG["MANUAL_HOLIDAYS"].copy()
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(HOLIDAYS_URL, headers=headers).json()
        for holiday in response["CM"]:
            # Convert from '26-Jan-2025' to '2025-01-26'
            date_obj = datetime.datetime.strptime(holiday["tradingDate"], "%d-%b-%Y").date()
            holidays.append(date_obj.strftime("%Y-%m-%d"))
    except Exception as e:
        notify_trader(subject="📅 Holiday Fetching Failed", body=f"Could not fetch holiday data:\n\n{str(e)}")
    return holidays


# 🧾 Logs trade activity with timestamp into the trade log file
def log_trade(tag, symbol, details):
    log_dir = CONFIG.get("LOG_DIR", ".")
    os.makedirs(log_dir, exist_ok=True)  # creates the folder if it doesn't exist

    log_path = os.path.join(log_dir, CONFIG["TRADE_LOG_FILE"])
    with open(log_path, "a") as f:
        f.write(f"{datetime.datetime.now()} - {tag} - {symbol} - {details}\n")



# ✅ Checks if the given date (or today) is a weekday (Mon–Fri)
def is_market_open(date=None):
    if date is None:
        date = datetime.date.today()
    return date.weekday() < 5


# 📈 Fetches the latest spot price for Nifty 50
def get_nifty_spot_price(fyers):
    response = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
    if response and "d" in response and len(response["d"]) > 0:
        return response["d"][0]["v"]["lp"]
    else:
        raise Exception("Failed to fetch Nifty spot price.")


# 🔄 Rounds any price to the nearest 50-point level (useful for ATM/OTM strikes)
def round_to_nearest_50(price):
    return round(price / 50) * 50


# 🏷️ Builds full FYERS symbol (like NSE:NIFTY25409CE) for a given strike and type
def get_option_symbol(strike_price, option_type):
    expiry = get_next_expiry_date()
    expiry_code = get_expiry_symbol_code(expiry)  # e.g., '25409'
    return f"NSE:NIFTY{expiry_code}{strike_price}{option_type}"


# 📅 Gets the nearest weekly expiry date adjusted for holidays/weekends
def get_next_expiry_date():
    today = datetime.date.today()

    # Read target expiry day from config
    expiry_day_name = CONFIG.get("STRATEGY_EXPIRY_DAY", "THURSDAY").upper()

    # Convert weekday name to number (e.g. "THURSDAY" → 3)
    day_map = {
        "MONDAY": 0,
        "TUESDAY": 1,
        "WEDNESDAY": 2,
        "THURSDAY": 3,
        "FRIDAY": 4
    }
    target_weekday = day_map.get(expiry_day_name, 3)  # default to Thursday if invalid

    # Calculate next occurrence of that weekday
    days_until_expiry = (target_weekday - today.weekday()) % 7
    expiry = today + datetime.timedelta(days=days_until_expiry)

    # If today is the expiry day but after 3:30 PM, move to next week
    if today.weekday() == target_weekday and datetime.datetime.now().time() > datetime.time(15, 30):
        expiry += datetime.timedelta(days=7)

    # Adjust for holidays/weekends
    while is_holiday(expiry) or expiry.weekday() >= 5:
        expiry -= datetime.timedelta(days=1)

    return expiry


# 🚀 Sends a market order to buy or sell a given symbol via FYERS API
def place_order(fyers, symbol, side, qty):
    if CONFIG["MODE"] == "TEST":
        print(f"[PAPER] Simulated {side} order for {qty} of {symbol}")
        return {
            "code": 1101,
            "message": "Simulated paper order (TEST mode)",
            "s": "ok",
            "id": f"PAPER_{int(time.time())}"
        }

    try:
        data = {
            "symbol": symbol,
            "qty": qty,
            "type": 2,
            "side": 1 if side == "BUY" else -1,
            "productType": "MARGIN",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderType": "MARKET"
        }
        response = fyers.place_order(data)
        return response  # ✅ Must return full response containing order 'id'
    except Exception as e:
        print(f"❌ Failed to place {side} order for {symbol}: {e}")
        return {"code": -1, "message": str(e), "s": "error"}



# 💣 Exits all open positions immediately via FYERS
def square_off_all_positions(fyers):
    try:
        return fyers.exit_positions({})
    except Exception as e:
        notify_trader(subject="❌ Square Off Failed", body=f"Square-off attempt failed:\n\n{str(e)}")
        return {"error": str(e)}


# 🔢 Converts an expiry date to FYERS symbol code format (like 25409 or 25O17)
def get_expiry_symbol_code(expiry_date):
    """
    Returns Fyers-formatted expiry code for option symbols.
    For example: 9th April 2025 → '25409', 17th Oct 2025 → '25O17'
    """
    month_map = {
        1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
        7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"
    }
    return f"{expiry_date.strftime('%y')}{month_map[expiry_date.month]}{expiry_date.day:02d}"


# 💾 Saves full bot state to JSON file
def get_state_path():
    log_dir = CONFIG.get("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, CONFIG["STATE_FILE"])


def save_state(state):
    with open(get_state_path(), "w") as f:
        json.dump(state, f)


def load_state():
    path = get_state_path()
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


# 🛑 Checks if the given date is a known market holiday
def is_holiday(date_obj):
    holidays = [datetime.datetime.strptime(d, "%Y-%m-%d").date() for d in fetch_market_holidays()]
    return date_obj in holidays


# 🧠 Calculates the strategy's starting day for the current expiry week
def get_strategy_entry_day():
    desired_day = CONFIG["STRATEGY_START_DAY"].upper()
    today = datetime.date.today()
    expiry = get_next_expiry_date()

    day_map = {
        "MONDAY": 0,
        "TUESDAY": 1,
        "WEDNESDAY": 2,
        "THURSDAY": 3,
        "FRIDAY": 4
    }

    target_weekday = day_map.get(desired_day, 1)  # default to Tuesday if invalid

    # Start from expiry and go backwards to find the desired day
    entry_day = expiry - datetime.timedelta(days=(expiry.weekday() - target_weekday))

    while is_holiday(entry_day) or entry_day.weekday() >= 5:
        entry_day -= datetime.timedelta(days=1)

    return entry_day


# 📦 Convenience function to save current state variables
def save_current_state(positions, booked_pnl, pnl_lock, trade_history, completed_legs, recovery_pending):
    save_state({
        "POSITIONS": positions,
        "BOOKED_PNL": booked_pnl,
        "PNL_LOCK": pnl_lock,
        "TRADE_HISTORY": trade_history,
        "COMPLETED_LEGS": completed_legs,
        "RECOVERY_PENDING": recovery_pending,
        "DATE": str(datetime.date.today())
    })


# 📊 Clean wrapper to fetch LTP of a symbol via FYERS quotes API
def get_ltp(fyers, symbol):
    """
    Fetch the latest traded price for a given symbol.
    Returns float or raises an error.
    """
    try:
        response = fyers.quotes({"symbols": symbol})
        return response["d"][0]["v"]["lp"]
    except Exception as e:
        raise ValueError(f"❌ Failed to fetch LTP for {symbol}: {e}")


# 📉 Calculates trailing SL based on entry and current LTP
def calculate_trailing_sl(entry_price, ltp, trailing_steps):
    """
    Returns updated SL based on current LTP and configured trailing thresholds.
    """
    updated_sl = entry_price * (1 + trailing_steps[0][1] / 100)  # fallback to initial SL
    for threshold, new_sl_pct in trailing_steps:
        if ltp < entry_price * (threshold / 100):
            updated_sl = entry_price * (1 + new_sl_pct / 100)
    return updated_sl


# 📅 Closes all positions on expiry and logs PnL
def handle_expiry_square_off(fyers, booked_pnl, pnl_lock, trade_history, completed_legs, positions, forced_exit_legs):
    square_off_all_positions(fyers)
    print("📅 Expiry day square-off complete.")
    log_trade("SquareOff", "All", "Positions squared off at expiry")

    for leg in list(positions.keys()):
        completed_legs.append(leg)
        forced_exit_legs.add(leg)
        log_trade(f"{leg}_FORCED_EXIT", positions[leg]["symbol"], f"Squared off at expiry")

    positions.clear()

    save_state({
        "POSITIONS": {},
        "BOOKED_PNL": booked_pnl,
        "PNL_LOCK": pnl_lock,
        "TRADE_HISTORY": trade_history,
        "COMPLETED_LEGS": completed_legs,
        "DATE": str(datetime.date.today())
    })

    print("✅ All positions cleared and state saved. Bot ready for next cycle.")



# 🔁 Resets bot state if the stored date doesn't match today's date
def reset_if_new_day(current_date, positions, booked_pnl, pnl_lock, trade_history, completed_legs):
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r") as f:
            prev_state = json.load(f)
        prev_date = prev_state.get("DATE", "")
        if prev_date != str(current_date):
            print(f"🧹 New day detected ({current_date}). Resetting bot state.")
            save_state({
                "POSITIONS": positions,
                "BOOKED_PNL": booked_pnl,
                "PNL_LOCK": pnl_lock,
                "TRADE_HISTORY": trade_history,
                "COMPLETED_LEGS": completed_legs,
                "DATE": str(current_date)
            })


# 🚫 Returns True if L1 and L2 already traded or week is completed
def should_skip_trading(trade_history, completed_legs, booked_pnl, positions):
    if set(["L1", "L2"]).issubset(trade_history):
        print("✅ L1 and L2 already traded. Skipping fresh entries.")

        # 👇 If recovery legs (L1.1, L2.1) are still active, continue monitoring
        if any(leg in positions for leg in ["L1.1", "L2.1"]):
            print("📊 Active recovery legs found. Will continue monitoring.")
            return False

        return True

    if {"L1", "L2"}.issubset(completed_legs):
        print(f"✅ Trading is done for the week. Booked PnL = ₹{booked_pnl:.2f}")
        log_trade("WEEK_DONE", "ALL", f"Booked PnL = ₹{booked_pnl:.2f}")
        return True

    return False


class BotState:
    def __init__(self):
        self.NIFTY_LTP = None
        self.PRINT_LTP = True
        self.POSITIONS = {}
        self.BOOKED_PNL = 0
        self.PNL_LOCK = 0
        self.TRADE_HISTORY = []
        self.COMPLETED_LEGS = []

    def to_dict(self):
        return {
            "POSITIONS": self.POSITIONS,
            "BOOKED_PNL": self.BOOKED_PNL,
            "PNL_LOCK": self.PNL_LOCK,
            "TRADE_HISTORY": self.TRADE_HISTORY,
            "COMPLETED_LEGS": self.COMPLETED_LEGS,
            "DATE": str(datetime.date.today())
        }

    def load_from_dict(self, data):
        self.POSITIONS = data.get("POSITIONS", {})
        self.BOOKED_PNL = data.get("BOOKED_PNL", 0)
        self.PNL_LOCK = data.get("PNL_LOCK", 0)
        self.TRADE_HISTORY = data.get("TRADE_HISTORY", [])
        self.COMPLETED_LEGS = data.get("COMPLETED_LEGS", [])


#Dynamic re entry system
def is_order_filled(fyers, order_id):
    try:
        order_book = fyers.orders()
        for order in order_book.get("orderBook", []):
            if order.get("id") == order_id:
                status = order.get("status")
                print(f"🔍 Order {order_id} status: {status}")
                return status == "TRADE"
        return False
    except Exception as e:
        print(f"Error checking order status for {order_id}: {e}")
        return False

def shutdown_watcher():
    shutdown_time = datetime.datetime.strptime(CONFIG["SHUTDOWN_TIME"], "%H:%M:%S").time()
    while True:
        now = datetime.datetime.now().time()
        if now >= shutdown_time:
            # Markdown formatted message
            shutdown_msg = """
अलविदा, काल मिलांगा, और बहुत कमाई करो!
               जय श्री राम🚩
"""
            print(f"⏹️ Shutdown time reached: {now}. Exiting bot.")
            # Assuming `notify_trader` supports Markdown and takes the correct `parse_mode` argument
            send_email(
                subject="*जय श्री राम*",
                body=shutdown_msg)
            send_telegram(shutdown_msg)
            os._exit(0)
        time.sleep(5)
