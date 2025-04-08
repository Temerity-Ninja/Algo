# config.py

CONFIG = {
    # Strategy Timings
    "ENTRY_TIME": "12:00:00",
    "SQUARE_OFF_TIME": "15:00:00",

    # MTM Profit Lock Settings
    "MTM_LOCK_BASE": 50000,
    "MTM_LOCK_INCREMENT": 10000,

    # Quantity per trade (Nifty 1 lot = 75 shares)
    "QTY": 75,

    # API and Environment
    "MODE": "LIVE",  # or "LIVE"

    # Retry Settings
    "RETRY_LIMIT": 5,

    # SMTP Email Alert Configuration
    "SMTP_EMAIL": "shriraminvestors@gmail.com",
    "SMTP_PASSWORD": "Bhav@343573",
    "ALERT_RECEIVER": "somanibhavyam@gmail.com",

    # Health Check
    "HEALTH_CHECK_INTERVAL": 5,  # 60 minutes

    # Holidays (Manual Overrides)
    "MANUAL_HOLIDAYS": ["2025-04-10", ],  # Example dates

    # Logging
    "TRADE_LOG_FILE": "trade_log.txt",

    # State Backup
    "STATE_FILE": "state.json",

    # Backup Directory
    "BACKUP_DIR": "backups",

    "TRADE_LOG_FILE": "/mnt/data/trade_log.txt",  # Adjusted for Colab


    # Other settings
    "ENABLE_MANUAL_OVERRIDE": True,

    # Strategy Parameters
    "ATM_OFFSET_PERCENT_INITIAL": 5.0,
    "ATM_OFFSET_PERCENT_RECOVERY": 2.75,

    "SL_INITIAL_PERCENT": 40,  # initial SL at +40% premium above entry
    "TARGET_INITIAL_PERCENT": 95,  # target is 95% premium decay

    "SL_RECOVERY_PERCENT": 39,  # recovery trades SL at +39%
    "TARGET_RECOVERY_PERCENT": 95,  # recovery target also at 95%

    # Trailing SL Steps: [(premium_threshold %, new SL % above entry)]
    "TRAILING_SL_STEPS_INITIAL": [
        (60, 30),  # Premium below 60% of entry, SL becomes entry +30%
        (20, 20)   # Premium below 20% of entry, SL becomes entry +20%
    ],

    "TRAILING_SL_STEPS_RECOVERY": [
        (90, 30),  # Recovery premium below 90%, SL becomes entry +10%
        (80, 20),
        (70, 10),
        (60, 0),
        (50, -10),
        (40, -20),
        (30,- 30),
        (20, -40),
        (10, -50),
        (5, -95)
    ],

    # Recovery trade waiting points
    "RECOVERY_TRADE_WAIT_POINTS": 8,

    # Strategy Start and Expiry
    "STRATEGY_START_DAY": "TUESDAY",  # Day after previous Thursday expiry
    "STRATEGY_EXPIRY_DAY": "THURSDAY"  # Expiry day (weekly)

    # URLs
HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master?type=trading"

"TRADE_LOG_FILE": "/mnt/data/trade_log.txt",  # Adjusted for Colab
}


