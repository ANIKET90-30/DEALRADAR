"""DealRadar: real game-price scraper + dashboard server. Python 3.8+, no dependencies.
Run:  python app.py   ->  http://localhost:8000
Data: CheapShark API (multi-store, USD) + Steam store API (INR price + genres)."""
import json, sqlite3, threading, time, os, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "deals.db")
INTERVAL = int(os.environ.get("SCRAPE_MINUTES", "60")) * 60
HOST, PORT = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
CS = "https://www.cheapshark.com/api/1.0"
STORES = [1, 3, 7, 11, 15, 25]  # Steam, GreenManGaming, GOG, Humble, Fanatical, Epic
STALE = 3 * 86400
state = {"running": False, "last": 0, "error": ""}

def http(u):
    r = urllib.request.Request(u, headers={"User-Agent": "DealRadar/1.0"})
    with urllib.request.urlopen(r, timeout=25) as f:
        return json.load(f)

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    c = db()
    c.executescript("""
CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY,title,thumb,steam_id,meta,low_ever REAL);
CREATE TABLE IF NOT EXISTS prices(gid TEXT,store TEXT,sale REAL,normal REAL,ts REAL,PRIMARY KEY(gid,store));
CREATE TABLE IF NOT EXISTS hist(gid TEXT,ts REAL,best REAL);
CREATE TABLE IF NOT EXISTS steam(gid TEXT PRIMARY KEY,genres TEXT,inr_sale REAL,inr_normal REAL);
CREATE TABLE IF NOT EXISTS wish(gid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS alerts(gid TEXT PRIMARY KEY,target REAL);
CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,name);""")
    try: c.execute("ALTER TABLE games ADD COLUMN low_ever REAL")
    except Exception: pass
    c.commit(); c.close()

def scrape():
    if state["running"]: return
    state.update(running=True, error="")
    try:
        c = db(); now = time.time()
        stores = http(CS + "/stores")
        for s in stores:
            c.execute("INSERT OR REPLACE INTO stores VALUES(?,?)", (s["storeID"], s["storeName"]))
        active = [s["storeID"] for s in stores if str(s.get("isActive")) == "1"]
        ids = set()
        for sid in active:  # 1) discover popular on-sale games from EVERY active store
            q = urllib.parse.urlencode(dict(storeID=sid, pageSize=30, onSale=1, upperPrice=80))
            try:
                for d in http(CS + "/deals?" + q):
                    c.execute("INSERT OR IGNORE INTO games(id,title,thumb,steam_id,meta) VALUES(?,?,?,?,?)",
                              (d["gameID"], d["title"], d["thumb"], d.get("steamAppID"), d.get("metacriticScore")))
                    ids.add(d["gameID"])
            except Exception as e: print("store", sid, "failed:", e)
            time.sleep(0.7)
        ids |= {r[0] for r in c.execute("SELECT gid FROM wish UNION SELECT gid FROM alerts")}
        c.commit(); ids = list(ids)
        for i in range(0, len(ids), 25):  # 2) fetch prices for each game across ALL stores + all-time low
            try: res = http(CS + "/games?ids=" + ",".join(ids[i:i + 25]))
            except Exception as e: print("batch failed:", e); continue
            for gid, g in res.items():
                ce = (g.get("cheapestPriceEver") or {}).get("price")
                c.execute("UPDATE games SET low_ever=? WHERE id=?", (float(ce) if ce else None, gid))
                for d in g.get("deals", []):
                    c.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?)",
                              (gid, d["storeID"], float(d["price"]), float(d["retailPrice"]), now))
            c.commit(); time.sleep(0.7)
        for (g,) in c.execute("SELECT DISTINCT gid FROM prices WHERE ts>=?", (now,)).fetchall():
            c.execute("INSERT INTO hist SELECT gid,?,MIN(sale) FROM prices WHERE gid=? AND ts>=?", (now, g, now))
        c.commit()
        # Steam: INR price + genres
        rows = c.execute("SELECT id,steam_id FROM games WHERE steam_id IS NOT NULL AND steam_id!='0' AND id IN "
                         "(SELECT gid FROM prices WHERE ts>=?) ORDER BY (id IN (SELECT gid FROM steam WHERE genres!='')) LIMIT 80", (now,)).fetchall()
        for r in rows:
            try:
                u = ("https://store.steampowered.com/api/appdetails?cc=in&filters=price_overview,genres&appids="
                     + str(r["steam_id"]))
                dt = http(u)[str(r["steam_id"])].get("data") or {}
                po = dt.get("price_overview") or {}
                gen = "|".join(x["description"] for x in dt.get("genres", []))
                old = c.execute("SELECT genres FROM steam WHERE gid=?", (r["id"],)).fetchone()
                c.execute("INSERT OR REPLACE INTO steam VALUES(?,?,?,?)",
                          (r["id"], gen or (old[0] if old else ""),
                           po["final"] / 100 if po else None, po["initial"] / 100 if po else None))
            except Exception: pass
            time.sleep(1.2)
        c.commit(); c.close()
        for g in snapshot():
            if g["triggered"]: print("ALERT: %s is $%.2f (target $%.2f)" % (g["title"], g["best"], g["alert"]))
        state["last"] = time.time()
    except Exception as e:
        state["error"] = str(e); print("scrape failed:", e)
    finally:
        state["running"] = False

def snapshot():
    c = db(); now = time.time(); out = []
    names = {r["id"]: r["name"] for r in c.execute("SELECT * FROM stores")}
    wish = {r[0] for r in c.execute("SELECT gid FROM wish")}
    al = {r[0]: r[1] for r in c.execute("SELECT * FROM alerts")}
    for g in c.execute("SELECT g.*,s.genres,s.inr_sale,s.inr_normal FROM games g LEFT JOIN steam s ON s.gid=g.id").fetchall():
        ps = c.execute("SELECT * FROM prices WHERE gid=? AND ts>?", (g["id"], now - STALE)).fetchall()
        if not ps: continue
        b = min(ps, key=lambda p: p["sale"]); normal = max(p["normal"] for p in ps)
        h = [r[0] for r in c.execute("SELECT best FROM hist WHERE gid=? ORDER BY ts DESC LIMIT 30", (g["id"],))][::-1]
        out.append(dict(
            id=g["id"], title=g["title"], thumb=g["thumb"], meta=g["meta"],
            genres=[x for x in (g["genres"] or "").split("|") if x] or ["Other"],
            stores=sorted([dict(name=names.get(p["store"], p["store"]), sale=p["sale"], normal=p["normal"]) for p in ps],
                          key=lambda s: s["sale"]),
            best=b["sale"], normal=normal, low_ever=g["low_ever"], discount=round((1 - b["sale"] / normal) * 100) if normal else 0,
            inr=[g["inr_sale"], g["inr_normal"]] if g["inr_sale"] else None, hist=h,
            lowest=(b["sale"] <= g["low_ever"] + 0.005) if g["low_ever"] else (len(h) > 2 and b["sale"] <= min(h)), wish=g["id"] in wish, alert=al.get(g["id"]),
            triggered=g["id"] in al and b["sale"] <= al[g["id"]]))
    c.close(); return out

class H(BaseHTTPRequestHandler):
    def send(self, code, body, ct="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/deals": return self.send(200, snapshot())
        if self.path == "/api/status": return self.send(200, state)
        if self.path in ("/", "/index.html"):
            return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        self.send(404, {})
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        c = db()
        if self.path == "/api/scrape": threading.Thread(target=scrape, daemon=True).start()
        elif self.path == "/api/wish":
            if d["on"]: c.execute("INSERT OR IGNORE INTO wish VALUES(?)", (d["id"],))
            else: c.execute("DELETE FROM wish WHERE gid=?", (d["id"],))
        elif self.path == "/api/alert":
            if d.get("target") is not None: c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)", (d["id"], d["target"]))
            else: c.execute("DELETE FROM alerts WHERE gid=?", (d["id"],))
        c.commit(); c.close(); self.send(200, {"ok": True})
    def log_message(self, *a): pass

def loop():
    while True:
        scrape(); time.sleep(INTERVAL)

if __name__ == "__main__":
    init(); threading.Thread(target=loop, daemon=True).start()
    print("DealRadar running at http://%s:%d  (first scrape takes ~2 min)" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
