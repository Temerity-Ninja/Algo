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
from config import CONFIG, HOLIDAYS_URL

# Authentication with FYERS
def getEncodedString(string):
    return base64.b64encode(str(string).encode("ascii")).decode("ascii")

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

        fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path=os.getcwd())
        return fyers

    except Exception as e:
        send_email_alert("Authentication Failed", str(e))
        raise

# Email Alert Utility
def send_email_alert(subject, message):
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(CONFIG["SMTP_EMAIL"], CONFIG["SMTP_PASSWORD"])
            smtp.sendmail(CONFIG["SMTP_EMAIL"], CONFIG["ALERT_RECEIVER"], f'Subject:{subject}\n\n{message}')
    except Exception as e:
        print(f"Failed to send email alert: {e}")

# Fetch Market Holidays
def fetch_market_holidays():
    holidays = CONFIG["MANUAL_HOLIDAYS"]
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(HOLIDAYS_URL, headers=headers).json()
        for holiday in response["CM"]:
            holidays.append(holiday["tradingDate"])
    except Exception as e:
        send_email_alert("Holiday Fetching Failed", str(e))
    return holidays

# Logging Utility
def log_trade(leg, symbol, details):
    timestamp = datetime.datetime.now()
    log_line = f"{timestamp} - {leg} - {symbol} - {details}\n"
    with open(CONFIG["TRADE_LOG_FILE"], "a") as file:
        file.write(log_line)

# Basic helper functions
def is_market_open(date=None):
    if date is None:
        date = datetime.date.today()
    return date.weekday() < 5

def get_next_trading_day(from_date=None):
    if from_date is None:
        from_date = datetime.date.today()
    next_day = from_date + datetime.timedelta(days=1)
    while not is_market_open(next_day):
        next_day += datetime.timedelta(days=1)
    return next_day

def get_nifty_spot_price(fyers):
    response = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
    if response and "d" in response and len(response["d"]) > 0:
        return response["d"][0]["v"]["lp"]
    else:
        raise Exception("Failed to fetch Nifty spot price.")

def round_to_nearest_50(price):
    return round(price / 50) * 50

def get_option_symbol(strike_price, option_type):
    expiry = get_next_expiry_date()
    expiry_str = expiry.strftime("%y%b%d").upper()
    return f"NIFTY{expiry_str}{strike_price}{option_type}"

def get_next_expiry_date():
    today = datetime.date.today()
    weekday = today.weekday()
    days_ahead = (3 - weekday) % 7
    expiry = today + datetime.timedelta(days=days_ahead)
    if expiry <= today:
        expiry += datetime.timedelta(days=7)
    return expiry
# Place order function to execute trades via Fyers API
def place_order(fyers, symbol, side, qty):
    data = {
        "symbol": symbol,
        "qty": qty,
        "type": 2,  # Market Order
        "side": 1 if side.upper() == "BUY" else -1,
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "orderTag": "algo"
    }
    response = fyers.place_order(data)
    return response

# Square off all open positions
def square_off_all_positions(fyers):
    try:
        return fyers.exit_positions({})
    except Exception as e:
        send_email_alert("Square Off Failed", str(e))
        return {"error": str(e)}

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
