"""
╔══════════════════════════════════════════════════════╗
║  SUPABASE_SYNC.PY — выгрузка данных для дашборда     ║
║  trade.syoma-dashboard.ru и агента-ревизора          ║
║                                                      ║
║  Раз в 5 минут отправляет в Supabase:                ║
║  • открытые позиции (реал + бумага)                  ║
║  • закрытые сделки (реал + бумага)                   ║
║  • снимок капитала реального счёта (раз в час)       ║
║  • пульс сервисов                                    ║
║  Только чтение локальных файлов бота, ордеров НЕТ.   ║
║  Если база недоступна — бот торгует как обычно.      ║
╚══════════════════════════════════════════════════════╝

Нужно в .env:
  SUPABASE_URL=https://vwjeknpjrujjfhuhquci.supabase.co
  SUPABASE_SECRET_KEY=sb_secret_...   (или legacy service_role JWT)
"""

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone

import requests

LIVE_STATE  = "logs/live_state.json"
LIVE_TRADES = "logs/live_trades.jsonl"
LIVE_CFG    = "LIVE_BREAKOUT.json"
PAPER_DIR   = os.getenv("PAPER_DIR", "/root/paper")
INTERVAL_S  = 300
LOG_F       = "logs/supabase_sync.log"


def _log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs("logs", exist_ok=True)
    with open(LOG_F, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


class Client:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.h = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):                      # legacy service_role JWT
            self.h["Authorization"] = f"Bearer {key}"

    def _req(self, method, table, params=None, body=None, prefer=None):
        h = dict(self.h)
        if prefer:
            h["Prefer"] = prefer
        r = requests.request(method, f"{self.base}/{table}", headers=h, params=params,
                             data=json.dumps(body) if body is not None else None, timeout=20)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {table}: HTTP {r.status_code} {r.text[:200]}")

    def upsert(self, table, rows, on_conflict, ignore=False):
        if rows:
            self._req("POST", table, {"on_conflict": on_conflict}, rows,
                      f"resolution={'ignore' if ignore else 'merge'}-duplicates,return=minimal")

    def delete(self, table, params):
        self._req("DELETE", table, params, prefer="return=minimal")


def _positions(source, state, now_iso):
    rows = []
    for sym, s in (state.get("symbols") or {}).items():
        p = s.get("pos")
        if not p:
            continue
        rows.append({"source": source, "symbol": sym, "side": "Buy" if p["d"] == 1 else "Sell",
                     "entry": p["entry"], "stop": p["stop"], "init_stop": p.get("init_stop"),
                     "qty": p.get("qty"), "risk_usdt": p.get("risk_usdt"),
                     "trailed": bool(p.get("trailed")),
                     "opened_at": _iso(p.get("entry_ts")), "updated_at": now_iso})
    return rows


def _trades(source, trades):
    rows = []
    for t in trades:
        rows.append({"source": source, "symbol": t["symbol"], "side": t["side"],
                     "reason": t.get("reason"), "entry": t["entry"], "exit": t["exit"],
                     "opened_at": _iso(t.get("t_open")), "closed_at": _iso(t["t_close"]),
                     "hours": t.get("hours"), "pnl_usdt": t.get("pnl_usdt"), "r": t["R"],
                     "risk_usdt": t.get("risk_usdt")})
    return rows


def _sync_positions(c, source, rows):
    c.upsert("positions", rows, "source,symbol")
    open_syms = [r["symbol"] for r in rows]
    params = {"source": f"eq.{source}"}
    if open_syms:
        params["symbol"] = f"not.in.({','.join(open_syms)})"
    c.delete("positions", params)


def _last_price(symbol):
    r = requests.get("https://api.bybit.com/v5/market/tickers",
                     params={"category": "linear", "symbol": symbol}, timeout=10).json()
    return float(r["result"]["list"][0]["lastPrice"])


def sync_once(c):
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    live_state = _read_json(LIVE_STATE, {})
    live_cfg = _read_json(LIVE_CFG, {})
    live_pos = _positions("live", live_state, now_iso)
    _sync_positions(c, "live", live_pos)
    c.upsert("trades", _trades("live", _read_jsonl(LIVE_TRADES)), "source,symbol,closed_at", ignore=True)

    unreal = 0.0
    for p in live_pos:
        try:
            d = 1 if p["side"] == "Buy" else -1
            unreal += d * (_last_price(p["symbol"]) - p["entry"]) * (p["qty"] or 0)
        except Exception:
            pass
    realized = live_state.get("realized_usdt", 0.0)
    capital = live_cfg.get("capital_usdt", 0)
    c.upsert("equity_snapshots", [{
        "ts": now.replace(minute=0, second=0, microsecond=0).isoformat(),
        "realized_usdt": round(realized, 4), "unrealized_usdt": round(unreal, 4),
        "total_equity": round(capital + realized + unreal, 4)}], "ts")

    beats = [{"service": "live_trader", "last_seen": now_iso, "info": {
        "enabled": bool(live_cfg.get("enabled")), "killed": bool(live_state.get("killed")),
        "symbols": live_cfg.get("symbols", []), "capital_usdt": capital,
        "risk_pct": live_cfg.get("risk_pct"), "risk_pct_by_symbol": live_cfg.get("risk_pct_by_symbol", {}),
        "max_drawdown_pct": live_cfg.get("max_drawdown_pct"), "open_positions": len(live_pos),
        "realized_usdt": realized, "started": _iso(live_state.get("started"))}}]

    paper_state = _read_json(os.path.join(PAPER_DIR, "paper_state.json"), None)
    if paper_state is not None:
        _sync_positions(c, "paper", _positions("paper", paper_state, now_iso))
        c.upsert("trades", _trades("paper", _read_jsonl(os.path.join(PAPER_DIR, "paper_trades.jsonl"))),
                 "source,symbol,closed_at", ignore=True)
        last = max([s.get("last_ts") or 0 for s in (paper_state.get("symbols") or {}).values()] or [0])
        beats.append({"service": "paper_trader", "last_seen": now_iso,
                      "info": {"last_bar": _iso(last), "started": _iso(paper_state.get("started"))}})

    beats.append({"service": "memecoin_bot", "last_seen": now_iso,
                  "info": {"paused": os.path.exists("PAUSE")}})
    c.upsert("heartbeat", beats, "service")


def _loop(c):
    _log("Синхронизация с Supabase запущена (раз в 5 мин)")
    fails = 0
    while True:
        try:
            sync_once(c)
            if fails:
                _log("Синхронизация восстановлена")
            fails = 0
        except Exception as e:
            fails += 1
            if fails in (1, 10) or fails % 100 == 0:
                _log(f"Ошибка синхронизации ({fails}): {e}\n{traceback.format_exc()}")
        time.sleep(INTERVAL_S)


def start():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        _log("SUPABASE_URL / SUPABASE_SECRET_KEY не заданы — дашборд не обновляется")
        return None
    th = threading.Thread(target=_loop, args=(Client(url, key),), name="supabase-sync", daemon=True)
    th.start()
    return th


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    c = Client(os.environ["SUPABASE_URL"],
               os.getenv("SUPABASE_SECRET_KEY") or os.environ["SUPABASE_SERVICE_KEY"])
    sync_once(c)
    print("ok")
