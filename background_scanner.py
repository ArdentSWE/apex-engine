import os
import asyncio
import datetime
import pytz
import json
import re
import aiohttp
import pandas as pd
import yfinance as yf
from anthropic import AsyncAnthropic

POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

TOP_100_TICKERS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD", "META", "AMZN", "GOOGL",
    "AVGO", "NFLX", "SMCI", "COST", "JPM", "WMT", "V", "MA", "XOM", "UNH",
    "JNJ", "PG", "HD", "PG", "ORCL", "CRM", "BAC", "ABBV", "CVX", "MRK",
    "KO", "CSCO", "IBM", "LIN", "ASML", "PEP", "TMO", "NOW", "DIS", "MCD",
    "INTC", "AMD", "INTU", "QCOM", "TXN", "AMAT", "MU", "PANW", "ADI", "KLAC",
    "LRCX", "SNPS", "CRWD", "FTNT", "PLTR", "SNOW", "ZS", "DDOG", "NET", "TEAM",
    "SHOP", "WDAY", "MDB", "SQ", "ROKU", "COIN", "PYPL", "HOOD", "MARA", "DKNG"
]

def fetch_spy_baseline():
    """Fetches the SPY 5-day return to calculate Relative Strength Divergence."""
    try:
        spy = yf.Ticker("SPY").history(period="5d")
        if len(spy) >= 5:
            return (spy['Close'].iloc[-1] - spy['Close'].iloc[0]) / spy['Close'].iloc[0]
    except Exception:
        pass
    return 0.0

async def compute_institutional_metrics(ticker: str, spy_5d_return: float):
    """Calculates SMC, Gamma Exposure, RS Divergence, and Delta Replacement."""
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        hist = stock.history(period="90d")
        if hist.empty or len(hist) < 30: return None
        
        close = hist['Close'].iloc[-1]
        
        # 1. SMC: Order Blocks & POC
        hist['Price_Bins'] = pd.cut(hist['Close'], bins=30)
        vol_profile = hist.groupby('Price_Bins', observed=False)['Volume'].sum()
        poc_price = vol_profile.idxmax().mid
        
        recent = hist.tail(30).copy()
        recent['Return'] = recent['Close'].pct_change()
        best_day_loc = recent.index.get_loc(recent['Return'].idxmax())
        bullish_ob = recent.iloc[best_day_loc - 1]['Low'] if best_day_loc > 0 and recent.iloc[best_day_loc - 1]['Close'] < recent.iloc[best_day_loc - 1]['Open'] else 0

        # 2. RS Divergence (Relative Strength)
        ticker_5d_return = (hist['Close'].iloc[-1] - hist['Close'].iloc[-5]) / hist['Close'].iloc[-5] if len(hist) >= 5 else 0
        rs_divergence = True if (spy_5d_return < -0.005 and ticker_5d_return > 0.01) else False
            
        # 3. Polygon Live Flow & Market Mechanics
        day_call_flow, day_put_flow = 0, 0
        swing_call_flow, swing_put_flow = 0, 0
        deep_itm_whale_flow = 0
        near_money_gamma = 0
        
        if POLYGON_KEY:
            min_strike, max_strike = close * 0.85, close * 1.15
            url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&limit=250&apiKey={POLYGON_KEY}"
            pages = 0
            
            async with aiohttp.ClientSession() as session:
                while url and pages < 10:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for c in data.get("results", []):
                                details = c.get('details', {})
                                ctype = details.get('contract_type', '').lower()
                                strike = details.get('strike_price', 0)
                                exp_str = details.get('expiration_date', '')
                                
                                vol = c.get('day', {}).get('volume', 0)
                                vwap = c.get('day', {}).get('vwap', 0)
                                oi = c.get('open_interest', 0)
                                greeks = c.get('greeks')
                                
                                if not exp_str or vol == 0 or not greeks: continue
                                
                                prem = vol * vwap * 100
                                delta = abs(greeks.get('delta', 0))
                                gamma = greeks.get('gamma', 0)
                                
                                exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                                dte = (exp_date - datetime.date.today()).days
                                
                                # A. DAY TRADE LOGIC (0-7 DTE & Gamma Squeeze)
                                if dte <= 7:
                                    if ctype == 'call': day_call_flow += prem
                                    else: day_put_flow += prem
                                    
                                    # Calculate Market Maker Hedging exposure near the money (within 5%)
                                    if abs(strike - close) / close < 0.05:
                                        contract_gex = gamma * oi * 100
                                        near_money_gamma += contract_gex if ctype == 'call' else -contract_gex

                                # B. SWING LOGIC (30-90 DTE)
                                elif 30 <= dte <= 90:
                                    if ctype == 'call': swing_call_flow += prem
                                    else: swing_put_flow += prem
                                    
                                # C. LEAP / WHALE LOGIC (>180 DTE & Deep ITM Delta)
                                elif dte >= 180 and delta >= 0.80 and ctype == 'call':
                                    deep_itm_whale_flow += prem
                                    
                            next_url = data.get("next_url")
                            url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                            pages += 1
                        else:
                            break
                            
        return {
            "ticker": ticker, "close": round(close, 2), "poc": round(poc_price, 2), "bullish_ob": round(bullish_ob, 2), 
            "rs_divergence": rs_divergence, "near_money_gamma": near_money_gamma,
            "day_call": day_call_flow, "day_put": day_put_flow,
            "swing_call": swing_call_flow, "swing_put": swing_put_flow,
            "deep_itm_whale": deep_itm_whale_flow
        }
    except Exception as e:
        return None

async def run_apex_scan():
    print(f"[{datetime.datetime.now()}] INITIATING MULTI-STRATEGY APEX SCAN...")
    
    spy_5d_return = fetch_spy_baseline()
    print(f"Macro Baseline: SPY 5D Return = {spy_5d_return * 100:.2f}%")
    
    sem = asyncio.Semaphore(50) 
    async def safe_compute(t):
        async with sem:
            return await compute_institutional_metrics(t, spy_5d_return)

    results = await asyncio.gather(*[safe_compute(t) for t in TOP_100_TICKERS])
    valid_data = [r for r in results if r is not None]

    day_cands, swing_cands, leap_cands = [], [], []

    for r in valid_data:
        # STRATEGY 1: Gamma Squeeze (Day Trades)
        # Extreme call flow on 0-7DTE + A massive positive Gamma Wall near the money
        total_day_flow = r['day_call'] + r['day_put']
        day_call_skew = r['day_call'] / total_day_flow if total_day_flow > 0 else 0
        
        if total_day_flow > 1000000 and day_call_skew > 0.65 and r['near_money_gamma'] > 50000:
            score = (total_day_flow * day_call_skew) + r['near_money_gamma']
            day_cands.append({"ticker": r['ticker'], "score": score, "data": r, "logic": "Gamma Squeeze"})

        # STRATEGY 2: Swing Accumulation
        # Catches either traditional SMC Order Block bounces OR Relative Strength Divergence
        total_swing_flow = r['swing_call'] + r['swing_put']
        swing_call_skew = r['swing_call'] / total_swing_flow if total_swing_flow > 0 else 0
        
        if total_swing_flow > 1500000 and swing_call_skew > 0.60:
            is_ob_bounce = r['bullish_ob'] > 0 and abs(r['close'] - r['bullish_ob']) / r['bullish_ob'] < 0.04
            if is_ob_bounce or r['rs_divergence']:
                logic_str = "RS Divergence" if r['rs_divergence'] else "SMC Order Block"
                swing_cands.append({"ticker": r['ticker'], "score": total_swing_flow, "data": r, "logic": logic_str})

        # STRATEGY 3: Deep ITM Stock Replacement
        # Catches >0.80 Delta Whale leaps. We don't care about puts, only institutional equity replacement.
        if r['deep_itm_whale'] > 3000000: 
            leap_cands.append({"ticker": r['ticker'], "score": r['deep_itm_whale'], "data": r, "logic": "0.80+ Delta Replacement"})

    top_days = [{"ticker": c['ticker'], "logic": c['logic'], "metrics": c['data']} for c in sorted(day_cands, key=lambda x: x['score'], reverse=True)[:10]]
    top_swings = [{"ticker": c['ticker'], "logic": c['logic'], "metrics": c['data']} for c in sorted(swing_cands, key=lambda x: x['score'], reverse=True)[:10]]
    top_leaps = [{"ticker": c['ticker'], "logic": c['logic'], "metrics": c['data']} for c in sorted(leap_cands, key=lambda x: x['score'], reverse=True)[:10]]
    
    if not top_days and not top_swings and not top_leaps:
        print("\nScan complete. No high-conviction mechanical setups found.")
        return

    print(f"\n[SCAN COMPLETE] Found {len(top_days)} Gamma Squeezes, {len(top_swings)} Swing Accumulations, and {len(top_leaps)} Delta Whales.")

    context_data = f"--- DAY TRADES (GAMMA SQUEEZE) ---\n{top_days}\n--- SWINGS (RS DIVERGENCE / SMC) ---\n{top_swings}\n--- LEAPS (DELTA REPLACEMENT) ---\n{top_leaps}"

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    sys_prompt = f"""
    You are the 'Apex Options Desk'. Convert these mathematically verified mechanical setups into the UI JSON payload.
    For the thesis, explicitly mention the 'logic' provided (e.g., 'Gamma Squeeze triggered by massive market maker hedging', or 'Deep ITM Delta replacement detected').
    
    Raw Telemetry:
    {context_data}
    
    Return ONLY a JSON object with a "plays" array containing objects with: 
    ticker, play_type (DAY TRADE, SWING, or LEAP), direction (CALLS), confidence (88-99), strike, expiration, thesis.
    """
    
    try:
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=4000,
            system=sys_prompt, messages=[{"role": "user", "content": "Generate JSON."}]
        )
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        clean_json = re.sub(r'```json\n|```', '', raw_text).strip()
        parsed_data = json.loads(clean_json)
        
        with open("latest_scans.json", "w") as f:
            json.dump(parsed_data, f, indent=4)
        print(f"✅ Mission Accomplished. latest_scans.json populated with Mechanical logic.")
            
    except Exception as e:
        print(f"Anthropic / JSON Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_apex_scan())