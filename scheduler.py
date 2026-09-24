import os
import json
import sqlite3
import requests
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler

load_dotenv()

DB_FILE = "carrier_data.db"
TOKEN_URL = "https://frontierstore.net"
CAPI_URL = "https://orerve.net"
HEADERS = {"User-Agent": "EDCD-CarrierUpkeepAlertManager-1.0.0"}

def refresh_user_tokens(callsign, refresh_token):
    """Exchanges an expired refresh token for a new valid pair and commits it to SQLite."""
    print(f"[{datetime.now()}] Refreshing token for CMDR Carrier: {callsign}")
    payload = {
        "grant_type": "refresh_token",
        "client_id": os.getenv("FRONTIER_CLIENT_ID"),
        "client_secret": os.getenv("FRONTIER_CLIENT_SECRET"),
        "refresh_token": refresh_token
    }
    
    res = requests.post(TOKEN_URL, data=payload, headers=HEADERS)
    if res.status_code != 200:
        print(f"Failed to refresh token for {callsign}: {res.text}")
        return None
        
    data = res.json()
    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token", refresh_token)
    
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            UPDATE users SET access_token = ?, refresh_token = ? WHERE callsign = ?
        """, (new_access, new_refresh, callsign))
        conn.commit()
        
    return new_access

def send_global_webhook(msg):
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if url:
        requests.post(url, json={"content": msg})

def run_global_maintenance_sweep():
    """Loops through all registered commanders in the DB to check thresholds."""
    print(f"[{datetime.now()}] Beginning multi-commander financial validation sweep...")
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT callsign, access_token, refresh_token, threshold_weeks FROM users")
        all_commanders = cursor.fetchall()

    for callsign, access_token, refresh_token, threshold in all_commanders:
        token = access_token
        res = requests.get(CAPI_URL, headers={**HEADERS, "Authorization": f"Bearer {token}"})
        
        # Auto handling for expired tokens per individual user row
        if res.status_code == 401:
            token = refresh_user_tokens(callsign, refresh_token)
            if not token:
                continue # Skip this user if refresh fails completely
            res = requests.get(CAPI_URL, headers={**HEADERS, "Authorization": f"Bearer {token}"})

        if res.status_code != 200:
            print(f"Error accessing CAPI endpoint data block for {callsign}")
            continue

        data = res.json()
        bank = data.get("balance", 0)
        
        # Calculate dynamic modules breakdown matrix
        weekly_bill = 5000000 
        for m, c in data.get("state", {}).get("services", {}).items():
            if c.get("status") == "active": weekly_bill += 5000000
            elif c.get("status") == "suspended": weekly_bill += 1500000

        runway = bank / weekly_bill if weekly_bill > 0 else float('inf')

        if runway <= threshold:
            send_global_webhook(
                f"🚨 **FLEET CARRIER PRE-MAINTENANCE ALARM** 🚨\n"
                f"Commander Fleet Unit: **{data.get('name')} ({callsign})**\n"
                f"• **Bank Balance:** {bank:,} CR\n"
                f"• **Weekly Upkeep:** {weekly_bill:,} CR\n"
                f"• **Runway Remaining:** {runway:.1f} weeks (Threshold limit configuration: {threshold} weeks)."
            )

if __name__ == "__main__":
    scheduler = BlockingScheduler()
    # Scans at 20:00 UTC every Wednesday evening prior to the Thursday 07:00 UTC maintenance tick
    scheduler.add_job(run_global_maintenance_sweep, 'cron', day_of_week='wed', hour=20, minute=0, timezone='UTC')
    print("Database Automated Alert Scheduler Worker Active...")
    scheduler.start()