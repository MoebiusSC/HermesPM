"""Hermes PM V1: public wallet research and forward-only paper execution."""
import json, os, re, sqlite3, threading, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = 'https://data-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
DB = os.getenv('DB_PATH', '/data/hermes_pm.sqlite3' if Path('/data').exists() else 'hermes_pm.sqlite3')
WALLET = re.compile(r'^0x[a-fA-F0-9]{40}$')
POLL = max(30, int(os.getenv('POLL_SECONDS', '90')))
START_CASH = float(os.getenv('START_CASH', '1000'))
MAX_TRADE = float(os.getenv('MAX_TRADE', '25'))
MAX_OPEN = int(os.getenv('MAX_OPEN', '8'))
MAX_SPREAD = float(os.getenv('MAX_SPREAD', '0.08'))
MAX_AGE = int(os.getenv('MAX_SIGNAL_AGE_SECONDS', '300'))
lock = threading.RLock()
status = {'last_poll': None, 'last_error': None, 'mode': 'paper-only'}


def connect():
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=20000')
    return db


def init():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, auto INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY, address TEXT NOT NULL, ts INTEGER NOT NULL, side TEXT NOT NULL, asset TEXT NOT NULL, title TEXT NOT NULL, outcome TEXT NOT NULL, source_price REAL NOT NULL, source_size REAL NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', seen_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT UNIQUE, address TEXT NOT NULL, asset TEXT NOT NULL, title TEXT NOT NULL, outcome TEXT NOT NULL, side TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL, cash_delta REAL NOT NULL, observed_at INTEGER NOT NULL, source_ts INTEGER NOT NULL, quote_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO settings VALUES ('cash', '1000');
        ''')
        if db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0:
            db.execute("UPDATE settings SET value=? WHERE key='cash'", (str(START_CASH),))


def api(host, path, params):
    url = host + path + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'HermesPM/1.0 research contact: local-owner', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=12) as res:
        return json.load(res)


def leaderboard():
    return api(BASE, '/v1/leaderboard', {'timePeriod': 'MONTH', 'orderBy': 'PNL', 'limit': 20, 'offset': 0})


def wallet_summary(address):
    closed = api(BASE, '/closed-positions', {'user': address, 'limit': 100, 'offset': 0})
    values = [float(p.get('realizedPnl') or 0) for p in closed]
    wins = sum(v > 0 for v in values)
    # Only sampled closed positions. Sum is not ROI and excludes open positions.
    return {'sample_count': len(values), 'sample_realized_pnl': round(sum(values), 2), 'sample_win_rate': round(wins/len(values), 3) if values else None,
            'coverage': 'últimas 100 posiciones cerradas; muestra parcial, no puntuación predictiva'}


def book_price(asset, side):
    book = api(CLOB, '/book', {'token_id': asset})
    levels = book.get('asks' if side == 'BUY' else 'bids') or []
    other = book.get('bids' if side == 'BUY' else 'asks') or []
    if not levels or not other:
        raise ValueError('sin contrapartida bid/ask')
    best = min(levels, key=lambda x: float(x['price'])) if side == 'BUY' else max(levels, key=lambda x: float(x['price']))
    opp = max(other, key=lambda x: float(x['price'])) if side == 'BUY' else min(other, key=lambda x: float(x['price']))
    ask = float(best['price']) if side == 'BUY' else float(opp['price'])
    bid = float(opp['price']) if side == 'BUY' else float(best['price'])
    if ask - bid > MAX_SPREAD:
        raise ValueError('spread demasiado amplio')
    return float(best['price']), float(best['size'])


def process_trade(db, address, trade, now, auto):
    side = str(trade.get('side', '')).upper()
    asset = str(trade.get('asset', ''))
    ts = int(trade.get('timestamp') or 0)
    source_price = float(trade.get('price') or 0)
    source_size = float(trade.get('size') or 0)
    if side not in ('BUY', 'SELL') or not asset.isdigit() or not 0 < source_price < 1 or source_size <= 0 or ts <= 0:
        return
    # Transaction hash alone can contain several fills; include stable trade fields.
    sid = '|'.join(map(str, [trade.get('transactionHash', ''), address, asset, side, ts, source_price, source_size]))
    if db.execute('SELECT 1 FROM signals WHERE id=?', (sid,)).fetchone():
        return
    age = now - ts
    state, reason = ('observed', 'wallet in manual mode') if not auto else ('skipped', 'unavailable')
    quote = None
    if auto:
        if age < 0 or age > MAX_AGE:
            reason = 'señal antigua; no se ejecuta retrospectivamente'
        elif side == 'SELL':
            reason = 'ventas automáticas desactivadas en V1; posiciones requieren cierre manual'
        else:
            try:
                price, available = book_price(asset, 'BUY')
                held = db.execute('SELECT COALESCE(SUM(shares),0) FROM fills WHERE asset=?', (asset,)).fetchone()[0]
                open_count = db.execute('SELECT COUNT(*) FROM (SELECT asset FROM fills GROUP BY asset HAVING SUM(shares)>0)').fetchone()[0]
                cash = float(db.execute("SELECT value FROM settings WHERE key='cash'").fetchone()[0])
                shares = min(MAX_TRADE/price, available, source_size, cash/price)
                if open_count >= MAX_OPEN and held <= 0: reason = 'límite de posiciones abiertas'
                elif shares*price < 1: reason = 'liquidez o efectivo insuficiente'
                elif price > source_price + 0.05: reason = 'precio empeoró más de 5 centavos'
                else:
                    quote = (price, shares)
                    state, reason = 'filled', 'paper fill al ask observado'
            except Exception as exc:
                reason = 'cotización no disponible: ' + str(exc)[:100]
    db.execute('INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (sid, address, ts, side, asset, str(trade.get('title', ''))[:220], str(trade.get('outcome', ''))[:60], source_price, source_size, state, reason, now))
    if quote:
        price, shares = quote
        db.execute('INSERT INTO fills(signal_id,address,asset,title,outcome,side,shares,price,cash_delta,observed_at,source_ts,quote_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid, address, asset, str(trade.get('title', ''))[:220], str(trade.get('outcome', ''))[:60], 'BUY', shares, price, -shares*price, now, ts, 'best ask at poll; no depth replay'))
        db.execute("UPDATE settings SET value=CAST(value AS REAL)-? WHERE key='cash'", (shares*price,))


def poll():
    now = int(time.time())
    with lock, connect() as db:
        wallets = db.execute('SELECT * FROM wallets WHERE enabled=1').fetchall()
        for wallet in wallets:
            try:
                trades = api(BASE, '/trades', {'user': wallet['address'], 'limit': 100, 'offset': 0, 'takerOnly': 'true'})
                for trade in sorted(trades, key=lambda t: int(t.get('timestamp') or 0)):
                    process_trade(db, wallet['address'], trade, now, bool(wallet['auto']))
                db.commit()
            except Exception as exc:
                status['last_error'] = f"{wallet['label']}: {exc}"[:240]
        status['last_poll'] = now


def worker():
    while True:
        try: poll()
        except Exception as exc: status['last_error'] = str(exc)[:240]
        time.sleep(POLL)


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def auth(self):
        key = os.getenv('HERMES_PM_KEY', '')
        if not key: return False
        import hmac
        return hmac.compare_digest(self.headers.get('X-Hermes-Key', ''), key)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/health': return self.respond(200, {'ok': True, 'mode': 'paper-only'})
        if path == '/':
            body = Path(__file__).with_name('static').joinpath('index.html').read_bytes()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        if not self.auth(): return self.respond(401, {'error': 'Clave requerida'})
        try:
            if path == '/api/leaderboard': return self.respond(200, leaderboard())
            if path.startswith('/api/wallet/'):
                address = path.rsplit('/', 1)[-1]
                if not WALLET.fullmatch(address): return self.respond(400, {'error': 'wallet inválida'})
                return self.respond(200, wallet_summary(address))
            if path == '/api/state':
                with lock, connect() as db:
                    wallets = [dict(x) for x in db.execute('SELECT * FROM wallets ORDER BY added_at DESC')]
                    signals = [dict(x) for x in db.execute('SELECT * FROM signals ORDER BY seen_at DESC LIMIT 100')]
                    fills = [dict(x) for x in db.execute('SELECT * FROM fills ORDER BY id DESC LIMIT 100')]
                    cash = float(db.execute("SELECT value FROM settings WHERE key='cash'").fetchone()[0])
                    positions = [dict(x) for x in db.execute('SELECT asset,title,outcome,SUM(shares) shares,SUM(-cash_delta) cost FROM fills GROUP BY asset HAVING SUM(shares)>0')]
                return self.respond(200, {'status': status, 'cash': cash, 'positions': positions, 'wallets': wallets, 'signals': signals, 'fills': fills,
                                          'note': 'P&L no calculado: posiciones sin marcado fiable a mercado. Fills usan ask observado, no garantizan ejecución real.'})
            return self.respond(404, {'error': 'ruta desconocida'})
        except Exception as exc: return self.respond(502, {'error': str(exc)[:200]})

    def do_POST(self):
        if not self.auth(): return self.respond(401, {'error': 'Clave requerida'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 4096: return self.respond(413, {'error': 'payload demasiado grande'})
            data = json.loads(self.rfile.read(length) or b'{}')
            path = urllib.parse.urlparse(self.path).path
            if path == '/api/wallets':
                address = str(data.get('address', '')).lower()
                if not WALLET.fullmatch(address): return self.respond(400, {'error': 'dirección EVM inválida'})
                label = str(data.get('label') or address[:10])[:50]
                with lock, connect() as db:
                    db.execute('INSERT INTO wallets(address,label,enabled,auto,added_at) VALUES(?,?,1,0,?) ON CONFLICT(address) DO UPDATE SET label=excluded.label,enabled=1', (address,label,int(time.time())))
                return self.respond(200, {'ok': True, 'auto': False})
            if path == '/api/wallet-mode':
                address = str(data.get('address','')).lower()
                if not WALLET.fullmatch(address): return self.respond(400, {'error': 'wallet inválida'})
                with lock, connect() as db:
                    db.execute('UPDATE wallets SET auto=? WHERE address=?', (1 if data.get('auto') is True else 0,address))
                return self.respond(200, {'ok': True})
            if path == '/api/remove-wallet':
                address = str(data.get('address','')).lower()
                with lock, connect() as db: db.execute('UPDATE wallets SET enabled=0,auto=0 WHERE address=?', (address,))
                return self.respond(200, {'ok': True})
            return self.respond(404, {'error': 'ruta desconocida'})
        except Exception as exc: return self.respond(400, {'error': str(exc)[:200]})


if __name__ == '__main__':
    init()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8000'))), Handler).serve_forever()
