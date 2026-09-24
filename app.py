import os
import sqlite3
import requests
from flask import Flask, redirect, request, session, url_for, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-key")

CLIENT_ID = os.getenv("FRONTIER_CLIENT_ID")
CLIENT_SECRET = os.getenv("FRONTIER_CLIENT_SECRET")
TOKEN_URL = "https://auth.frontierstore.net"
CAPI_URL = "https://companion.orerve.net"
REDIRECT_URI = "https://localhost/callback"
HEADERS = {"User-Agent": "EDCD-CarrierUpkeepAlertManager-1.0.0"}
DB_FILE = "carrier_data.db"

CORE_CARRIER_UPKEEP = 5000000
MODULE_COSTS = {
    "refuel": {"ok": 1500000, "disabled": 750000, "not-installed": 0},
    "repair": {"ok": 1500000, "disabled": 750000, "not-installed": 0},
    "rearm": {"ok": 1500000, "disabled": 750000, "not-installed": 0},
    "cartographics": {"ok": 1850000, "disabled": 700000, "not-installed": 0},
    "shipyard": {"ok": 6500000, "disabled": 1800000, "not-installed": 0},
    "outfitting": {"ok": 5000000, "disabled": 1500000, "not-installed": 0},
    "voucherredemption": {"ok": 1850000, "disabled": 850000, "not-installed": 0},
    "securetrading": {"ok": 2000000, "disabled": 1250000, "not-installed": 0},
    "bartender": {"ok": 1750000, "disabled": 1250000, "not-installed": 0},
    "genommics": {"ok": 1500000, "disabled": 700000, "not-installed": 0},
    "pioneersupplies": {"ok": 5000000, "disabled": 1500000, "not-installed": 0},
}

def init_db():
    """Initializes the database schema if it does not already exist."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                callsign TEXT PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                threshold_weeks INTEGER DEFAULT 4
            )
        """)
        conn.commit()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    params = {
        "audience": "all",
        "scope": "auth capi",
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI
    }
    return redirect(f"{TOKEN_URL}/auth?{requests.compat.urlencode(params)}")

@app.route('/callback')
def callback():
    code = request.args.get('code')
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code
    }
    resr = requests.post(f"{TOKEN_URL}/token", data=payload, headers=HEADERS)
    res = resr.json()
    
    access_token = res.get("access_token")
    refresh_token = res.get("refresh_token")
    
    # Temporarily fetch carrier info to get the unique key (Callsign)
    capi = requests.get(f"{CAPI_URL}/profile", headers={**HEADERS, "Authorization": f"Bearer {access_token}"})
    capi_res = capi.json()
    callsign = capi_res.get("commander").get("name")
    
    # Save or update user inside the SQLite database
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO users (callsign, access_token, refresh_token) 
            VALUES (?, ?, ?)
            ON CONFLICT(callsign) DO UPDATE SET 
                access_token=excluded.access_token, 
                refresh_token=excluded.refresh_token
        """, (callsign, access_token, refresh_token))
        conn.commit()
        
    session['user_callsign'] = callsign
    session['access_token'] = access_token
    return redirect(url_for('index'))

@app.route('/save-config', methods=['POST'])
def save_config():
    callsign = session.get('user_callsign')
    if not callsign:
        return jsonify({"error": "Unauthorized"}), 401
        
    weeks = int(request.json.get("threshold_weeks", 4))
    
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET threshold_weeks = ? WHERE callsign = ?", (weeks, callsign))
        conn.commit()
        
    return jsonify({"status": "success"})

@app.route('/upkeep')
def calculate_upkeep():
    callsign = session.get('user_callsign')
    token = session.get('access_token')
    
    if not callsign or not token:
        return jsonify({"error": "Unauthorized"}), 401

    res = requests.get(F"{CAPI_URL}/fleetcarrier", headers={**HEADERS, "Authorization": f"Bearer {token}"})
    if res.status_code == 401:
        return jsonify({"error": "Token expired"}), 401

    data = res.json()
    carrier_bank = int(data.get("balance", 0))
    services = data.get("servicesCrew", {})
    
    weekly_total = CORE_CARRIER_UPKEEP
    module_summary = {}

    for m, c in MODULE_COSTS.items():
        if m in services:
            st = services.get(m).get("status")
            cost = c[st]
            weekly_total += cost
            module_summary[m] = {"state": st, "cost_cr": cost}
        else:
            module_summary[m] = {"state": "not-installed", "cost_cr": 0}


    runway = carrier_bank / weekly_total if weekly_total > 0 else float('inf')
    
    # Grab personalized user configurations from database
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT threshold_weeks FROM users WHERE callsign = ?", (callsign,))
        row = cursor.fetchone()
        threshold = row[0] if row else 4
    
    alert_tier = "HEALTHY"
    msg = f"Your carrier asset reserves remain operational for {int(runway)} weeks."
    if runway <= threshold:
        alert_tier = "CRITICAL_WARNING"
        msg = f"DANGER: Balance tracks below your user threshold configuration of {threshold} weeks!"

    return jsonify({
        "carrier_name": data.get("name").get("vanityName"), "callsign": callsign,
        "carrier_bank_balance": carrier_bank, "calculated_weekly_upkeep": weekly_total,
        "weeks_remaining_until_debt": round(runway, 1), "alert_tier": alert_tier,
        "alert_details": msg, "modules_status": module_summary,
        "user_configured_threshold": threshold
    })

if __name__ == '__main__':
    init_db()
    app.run(port=5000, debug=True)