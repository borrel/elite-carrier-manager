require('dotenv').config();
const express = require('express');
const session = require('express-session');
const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const cron = require('node-cron');

const app = express();
const PORT = process.env.PORT || 5000;
const DB_FILE = "carrier_data.db";

// Frontier API Configuration Constants
const CLIENT_ID = process.env.FRONTIER_CLIENT_ID;
const CLIENT_SECRET = process.env.FRONTIER_CLIENT_SECRET;
const REDIRECT_URI = process.env.REDIRECT_URI || "http://localhost:5000/callback";
const TOKEN_URL = "https://frontierstore.net";
const CAPI_URL = "https://orerve.net";
const USER_AGENT = "EDCD-CarrierUpkeepAlertManager-1.0.0";

const CORE_CARRIER_UPKEEP = 5000000;
const MODULE_COSTS = {
    "refuel": { "enabled": 5000000, "disabled": 1500000, "not-installed": 0 },
    "repair": { "enabled": 5000000, "disabled": 1500000, "not-installed": 0 },
    "armory": { "enabled": 5000000, "disabled": 1500000, "not-installed": 0 },
    "cartographics": { "enabled": 7000000, "disabled": 2500000, "not-installed": 0 },
    "shipyard": { "enabled": 7000000, "disabled": 2500000, "not-installed": 0 },
    "outfitting": { "enabled": 5000000, "disabled": 1500000, "not-installed": 0 }
};

// Express Setup Configuration
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(session({
    secret: process.env.FLASK_SECRET_KEY || 'node-carrier-secret',
    resave: false,
    saveUninitialized: true
}));

// Initialize SQLite database connection framework
const db = new sqlite3.Database(DB_FILE, (err) => {
    if (err) console.error("Database connection failure:", err.message);
    db.run(`CREATE TABLE IF NOT EXISTS users (
        callsign TEXT PRIMARY KEY,
        access_token TEXT,
        refresh_token TEXT,
        threshold_weeks INTEGER DEFAULT 4
    )`);
});

// Helper for HTTP operations targeting Frontier endpoints
async function apiRequest(url, options = {}) {
    const response = await fetch(url, {
        ...options,
        headers: { "User-Agent": USER_AGENT, ...options.headers }
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
}

// ---------------- PLATFORM ROUTES ----------------

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'templates', 'index.html'));
});

app.get('/login', (req, res) => {
    const params = new URLSearchParams({
        audience: "all",
        scope: "auth capi text",
        response_type: "code",
        client_id: CLIENT_ID,
        redirect_uri: REDIRECT_URI
    });
    res.redirect(`https://frontierstore.net{params.toString()}`);
});

app.get('/callback', async (req, res) => {
    const code = req.query.code;
    if (!code) return res.status(400).send("Authentication canceled.");

    try {
        const tokenData = await apiRequest(TOKEN_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({
                grant_type: "authorization_code",
                client_id: CLIENT_ID,
                client_secret: CLIENT_SECRET,
                redirect_uri: REDIRECT_URI,
                code: code
            })
        });

        const carrierData = await apiRequest(CAPI_URL, {
            headers: { "Authorization": `Bearer ${tokenData.access_token}` }
        });
        const callsign = carrierData.callsign || "UNKNOWN";

        db.run(`INSERT INTO users (callsign, access_token, refresh_token) VALUES (?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET access_token=excluded.access_token, refresh_token=excluded.refresh_token`,
            [callsign, tokenData.access_token, tokenData.refresh_token]);

        req.session.user_callsign = callsign;
        req.session.access_token = tokenData.access_token;
        res.redirect('/');
    } catch (err) {
        res.status(500).send(`Token handshaking verification error: ${err.message}`);
    }
});

app.post('/save-config', (req, res) => {
    const callsign = req.session.user_callsign;
    if (!callsign) return res.status(401).json({ error: "Unauthorized" });

    const weeks = parseInt(req.body.threshold_weeks || 4);
    db.run("UPDATE users SET threshold_weeks = ? WHERE callsign = ?", [weeks, callsign], (err) => {
        if (err) return res.status(500).json({ error: err.message });
        res.json({ status: "success" });
    });
});

app.get('/upkeep', async (req, res) => {
    const callsign = req.session.user_callsign;
    const token = req.session.access_token;
    if (!callsign || !token) return res.status(401).json({ error: "Unauthorized" });

    try {
        const data = await apiRequest(CAPI_URL, { headers: { "Authorization": `Bearer ${token}` } });
        const bank = data.balance || 0;
        const services = data.state?.services || {};

        let weeklyTotal = CORE_CARRIER_UPKEEP;
        let moduleSummary = {};

        Object.entries(services).forEach(([m, config]) => {
            if (MODULE_COSTS[m]) {
                const st = config.status === "active" ? "enabled" : config.status === "suspended" ? "disabled" : "not-installed";
                const cost = MODULE_COSTS[m][st];
                weeklyTotal += cost;
                moduleSummary[m] = { state: st, cost_cr: cost };
            }
        });

        const runway = weeklyTotal > 0 ? bank / weeklyTotal : Infinity;

        db.get("SELECT threshold_weeks FROM users WHERE callsign = ?", [callsign], (err, row) => {
            const threshold = row ? row.threshold_weeks : 4;
            let alertTier = "HEALTHY";
            let msg = `Your carrier asset reserves remain operational for ${Math.floor(runway)} weeks.`;

            if (runway <= threshold) {
                alertTier = "CRITICAL_WARNING";
                msg = `DANGER: Balance tracks below your user threshold configuration of ${threshold} weeks!`;
            } else if (runway <= 12) {
                alertTier = "LOW_FUNDS_ALERT";
                msg = "Warning: Mid-tier operational asset drainage detected.";
            }

            res.json({
                carrier_name: data.name, callsign,
                carrier_bank_balance: bank, calculated_weekly_upkeep: weeklyTotal,
                weeks_remaining_until_debt: Math.round(runway * 10) / 10, alert_tier: alertTier,
                alert_details: msg, modules_status: moduleSummary, user_configured_threshold: threshold
            });
        });
    } catch (err) {
        res.status(401).json({ error: "Session expired or communication error." });
    }
});

// ---------------- BACKGROUND AUTOMATION ENGINE ----------------

async function refreshUserTokens(callsign, refreshToken) {
    try {
        const res = await apiRequest(TOKEN_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({
                grant_type: "refresh_token",
                client_id: CLIENT_ID,
                client_secret: CLIENT_SECRET,
                refresh_token: refreshToken
            })
        });
        db.run("UPDATE users SET access_token = ?, refresh_token = ? WHERE callsign = ?", [res.access_token, res.refresh_token || refreshToken, callsign]);
        return res.access_token;
    } catch (e) {
        console.error(`Token update dropped for unit profile line: ${callsign}`, e.message);
        return null;
    }
}

async function runGlobalMaintenanceSweep() {
    console.log(`[${new Date().toISOString()}] Launching pre-maintenance financial validation sweep...`);
    db.all("SELECT callsign, access_token, refresh_token, threshold_weeks FROM users", [], async (err, rows) => {
        if (err || !rows) return;

        for (const user of rows) {
            let token = user.access_token;
            let data;
            try {
                data = await apiRequest(CAPI_URL, { headers: { "Authorization": `Bearer ${token}` } });
            } catch (e) {
                token = await refreshUserTokens(user.callsign, user.refresh_token);
                if (!token) continue;
                try {
                    data = await apiRequest(CAPI_URL, { headers: { "Authorization": `Bearer ${token}` } });
                } catch (err) { continue; }
            }

            const bank = data.balance || 0;
            let weeklyBill = CORE_CARRIER_UPKEEP;
            Object.entries(data.state?.services || {}).forEach(([m, c]) => {
                if (c.status === "active") weeklyBill += 5000000;
                else if (c.status === "suspended") weeklyBill += 1500000;
            });

            const runway = weeklyBill > 0 ? bank / weeklyBill : Infinity;
            if (runway <= user.threshold_weeks && process.env.DISCORD_WEBHOOK_URL) {
                await fetch(process.env.DISCORD_WEBHOOK_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        content: `🚨 **FLEET CARRIER PRE-MAINTENANCE ALARM** 🚨\nCarrier: **${data.name} (${user.callsign})**\n• Balance: ${bank.toLocaleString()} CR\n• Runway Remaining: ${runway.toFixed(1)} weeks.`
                    })
                });
            }
        }
    });
}

// CRON JOB SETUP: Runs at 20:00 UTC every Wednesday evening 
// (Cron format pattern sequence string parameters format: minute hour day-of-month month day-of-week)
cron.schedule('0 20 * * 3', () => {
    runGlobalMaintenanceSweep();
}, { timezone: "UTC" });

app.listen(PORT, () => console.log(`Node app container tracking engine active across port link: ${PORT}`));