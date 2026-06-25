#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
funding_monitor.py — GATE 1 / Модуль A: Funding-carry радар (paper, $0, read-only)
=================================================================================
Источник: Hyperliquid public API (без ключа).
  - metaAndAssetCtxs  -> funding (hourly) + mark + open interest по 230+ рынкам
  - predictedFundings -> кросс-венью funding (Hyperliquid / Binance / Bybit)

ДВА СИГНАЛА:
  1) SINGLE — дельта-нейтраль спот+перп на одной площадке: держишь спот,
     шортишь перп (или наоборот), собираешь funding. APY после комиссий + breakeven.
  2) XVENUE — funding-спред между площадками: лонг перп там, где funding ниже,
     шорт перп там, где выше. Нейтрально обеими ногами, собираешь спред.

Принцип честности: только paper-лог. Капитал — после подтверждённой стабильности.
"""
import os, sys, csv, json, time, argparse
from datetime import datetime, timezone
import requests

STATE_DIR = os.getenv("YIELD_STATE_DIR", os.getenv("ARB_STATE_DIR", "./state"))
os.makedirs(STATE_DIR, exist_ok=True)
def P(n): return os.path.join(STATE_DIR, n)

HL = "https://api.hyperliquid.xyz/info"
HTTP_TIMEOUT = 30
SCAN_INTERVAL = 900  # 15 мин для loop-режима

# --- модель издержек / пороги ---
MIN_OI_USD        = 1_000_000   # только ликвидные рынки
ROUND_TRIP_FEE    = 0.0020      # ~0.20% на открыть+закрыть обе ноги (taker+спот), консервативно
MIN_APY_SINGLE    = 15.0        # % APY для actionable одиночного carry
MIN_SPREAD_APY    = 20.0        # % APY для actionable кросс-венью спреда
HOURS_Y = 24 * 365

def hl_post(payload):
    r = requests.post(HL, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def to_apy_hourly(hourly_rate):  # доля за час -> % APY
    return hourly_rate * HOURS_Y * 100

def scan_once():
    ts = datetime.now(timezone.utc).isoformat()
    # ---------- 1) SINGLE carry ----------
    single_rows = []
    try:
        meta, ctxs = hl_post({"type": "metaAndAssetCtxs"})
        for u, c in zip(meta["universe"], ctxs):
            f = c.get("funding")
            mark = c.get("markPx")
            oi = c.get("openInterest")
            if f is None or mark is None or oi is None:
                continue
            f = float(f); mark = float(mark); oi_usd = float(oi) * mark
            if oi_usd < MIN_OI_USD:
                continue
            apy = to_apy_hourly(f)               # знак: + => шорт получает; - => лонг получает
            daily = abs(apy) / 365.0
            breakeven_days = (ROUND_TRIP_FEE * 100) / daily if daily > 1e-9 else 9999
            net_apy = abs(apy) - (ROUND_TRIP_FEE * 100) * 365 / 30  # амортизация комиссий за ~30 дней удержания
            side = "short_perp+spot_long" if apy > 0 else "long_perp+spot_short"
            actionable = int(abs(apy) >= MIN_APY_SINGLE and breakeven_days <= 7)
            single_rows.append([ts, u["name"], round(f*100, 5), round(apy, 2),
                                round(oi_usd), side, round(breakeven_days, 2),
                                round(net_apy, 2), actionable])
    except Exception as e:
        print(f"  ! SINGLE упал: {e}")

    # ---------- 2) XVENUE спред ----------
    x_rows = []
    try:
        pf = hl_post({"type": "predictedFundings"})
        for asset, venues in pf:
            quotes = {}
            for vname, vdata in venues:
                if not vdata:
                    continue
                rate = vdata.get("fundingRate")
                iv = vdata.get("fundingIntervalHours")
                if rate is None or not iv:
                    continue
                hourly = float(rate) / float(iv)
                quotes[vname] = to_apy_hourly(hourly)
            if len(quotes) < 2:
                continue
            hi_v = max(quotes, key=quotes.get); lo_v = min(quotes, key=quotes.get)
            spread = quotes[hi_v] - quotes[lo_v]    # шорт на hi + лонг на lo = собираешь спред
            net_spread = spread - (ROUND_TRIP_FEE * 100) * 365 / 30
            actionable = int(spread >= MIN_SPREAD_APY)
            x_rows.append([ts, asset, round(quotes.get("HlPerp", float("nan")), 2),
                           hi_v, round(quotes[hi_v], 2), lo_v, round(quotes[lo_v], 2),
                           round(spread, 2), round(net_spread, 2), actionable])
    except Exception as e:
        print(f"  ! XVENUE упал: {e}")

    # ---------- запись ----------
    _append("funding_single_log.csv",
            ["ts","asset","funding_hourly_pct","funding_apy_pct","oi_usd","side",
             "breakeven_days","net_apy_30d_pct","actionable"], single_rows)
    _append("funding_xvenue_log.csv",
            ["ts","asset","hl_apy_pct","hi_venue","hi_apy_pct","lo_venue","lo_apy_pct",
             "spread_apy_pct","net_spread_apy_pct","actionable"], x_rows)

    s_act = sum(r[-1] for r in single_rows); x_act = sum(r[-1] for r in x_rows)
    print(f"=== FUNDING {ts} ===")
    print(f"  SINGLE: рынков {len(single_rows)} | actionable {s_act}")
    for r in sorted(single_rows, key=lambda x: -abs(x[3]))[:5]:
        print(f"     {r[1]:8} apy={r[3]:+.1f}% {r[5]} breakeven={r[6]}d oi=${r[4]:,}")
    print(f"  XVENUE: активов {len(x_rows)} | actionable {x_act}")
    for r in sorted(x_rows, key=lambda x: -x[7])[:5]:
        print(f"     {r[1]:8} spread={r[7]:+.1f}%APY  short@{r[3]}({r[4]:+.1f}) long@{r[5]}({r[6]:+.1f})")

def _append(fname, header, rows):
    new = not os.path.exists(P(fname))
    with open(P(fname), "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(header)
        for r in rows: w.writerow(r)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--burst", type=int, default=0)
    ap.add_argument("--interval", type=int, default=90)
    a = ap.parse_args()
    print(f"funding_monitor GATE1/A | Hyperliquid | state={STATE_DIR}")
    if a.burst > 0:
        for i in range(a.burst):
            scan_once()
            if i < a.burst-1: time.sleep(a.interval)
        return
    if a.once:
        scan_once(); return
    while True:
        try: scan_once()
        except KeyboardInterrupt: print("\nстоп."); break
        except Exception as e: print(f"  ! проход упал: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
