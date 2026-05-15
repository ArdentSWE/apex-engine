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

# THE UNIVERSE: Expand this to whatever your paid tier can handle.
TOP_100_TICKERS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD", "META", "AMZN", "GOOGL",
    "AVGO", "NFLX", "SMCI", "COST", "JPM", "WMT", "V", "MA", "XOM", "UNH",
    "JNJ", "PG", "HD", "PG", "ORCL", "CRM", "BAC", "ABBV", "CVX", "MRK",
    "KO", "CSCO", "IBM", "LIN", "ASML", "PEP", "TMO", "NOW", "DIS", "MCD",
    "INTC", "AMD", "INTU", "QCOM", "TXN", "AMAT", "MU", "PANW", "ADI", "KLAC",
    "LRCX", "SNPS", "CRWD", "FTNT", "PLTR", "SNOW", "ZS", "DDOG", "NET", "TEAM",
    "SHOP", "WDAY", "MDB", "SQ", "ROKU", "COIN", "PYPL", "HOOD", "MARA", "DKNG"
]

async def compute_smc_data(ticker: str):
    """Calculates true Volume POC, Order Blocks, and Flow for a ticker."""
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        hist = stock.history(period="90d")
        if hist.empty or len(hist) < 30: return None
        
        close = hist['Close'].iloc[-1]
        
        # 1. Volume Point of Control (POC)
        hist['Price_Bins'] = pd.cut(hist['Close'], bins=30)
        vol_profile = hist.groupby('Price_Bins', observed=False)['Volume'].sum()
        poc_price = vol_profile.idxmax().mid
        
        # 2. Order Blocks
        recent = hist.tail(30).copy()
        recent['Return'] = recent['Close'].pct_change()
        best_day_loc = recent.index.get_loc(recent['Return'].idxmax())
        
        bullish_ob = 0
        if best_day_loc > 0:
            ob_candle = recent.iloc[best_day_loc - 1]
            if ob_candle['Close'] < ob_candle['Open']: bullish_ob = ob_candle['Low']
            
        # 3. Polygon Live Flow
        call_prem, put_prem = 0, 0
        if POLYGON_KEY:
            url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?limit=250&apiKey={POLYGON_KEY}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("results", []):
                            ctype = c.get('details', {}).get('contract_type', '').lower()
                            vol = c.get('day', {}).get('volume', 0)
                            vwap = c.get('day', {}).get('vwap', 0)
                            prem = vol * vwap * 100
                            if ctype == 'call': call_prem += prem
                            elif ctype == 'put': put_prem += prem
                            
        return {
            "ticker": ticker, "close": round(close, 2), "poc": round(poc_price, 2),
            "bullish_ob": round(bullish_ob, 2), "call_prem": call_prem, "put_prem": put_prem
        }
    except Exception as e:
        print(f"SMC Error for {ticker}: {e}")
        return None

async def run_apex_scan():
    print(f"[{datetime.datetime.now()}] INITIATING PAID TIER APEX SCAN...")
    
    # PAID TIER: We can hit 50 concurrent requests easily
    sem = asyncio.Semaphore(50) 
    async def safe_compute(t):
        async with sem:
            return await compute_smc_data(t)

    results = await asyncio.gather(*[safe_compute(t) for t in TOP_100_TICKERS])
    valid_data = [r for r in results if r is not None]

    day_cands, swing_cands, leap_cands = [], [], []

    for r in valid_data:
        close, poc, bull_ob = r['close'], r['poc'], r['bullish_ob']
        call_p, put_p = r['call_prem'], r['put_prem']
        total_flow = call_p + put_p

        if total_flow == 0: continue

        call_skew = call_p / total_flow if total_flow > 0 else 0
        whale_multiplier = 2.0 if total_flow > 10000000 else 1.0

        # Day Trade: Near POC + High Skew
        if abs(close - poc) / poc < 0.02 and call_skew > 0.60:
            day_cands.append({"ticker": r['ticker'], "score": (total_flow * call_skew) * whale_multiplier, "data": r})

        # Swing: Near OB + High Skew
        if bull_ob > 0 and abs(close - bull_ob) / bull_ob < 0.035 and call_skew > 0.55:
            swing_cands.append({"ticker": r['ticker'], "score": total_flow * whale_multiplier, "data": r})

        # Leap/Whales: Pure massive flow
        if total_flow > 2000000:
            leap_cands.append({"ticker": r['ticker'], "score": total_flow * whale_multiplier, "data": r})

    # Capture the top 15 in each category to ensure nothing slips through
    top_days = [c['data'] for c in sorted(day_cands, key=lambda x: x['score'], reverse=True)[:15]]
    top_swings = [c['data'] for c in sorted(swing_cands, key=lambda x: x['score'], reverse=True)[:15]]
    top_leaps = [c['data'] for c in sorted(leap_cands, key=lambda x: x['score'], reverse=True)[:15]]
    
    if not top_days and not top_swings and not top_leaps:
        print("Scan complete. No high-probability setups found right now.")
        return

    context_data = f"--- DAY TRADES ---\n{top_days}\n--- SWINGS ---\n{top_swings}\n--- LEAPS/WHALES ---\n{top_leaps}"

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    sys_prompt = f"""
    You are the 'Apex Options Desk'. Convert the following math-verified setups into the UI JSON payload.
    Do NOT miss any tickers. Emphasize whale sweeps >$10M.
    {context_data}
    
    Return ONLY a JSON object with a "plays" array containing objects with: 
    ticker, play_type (DAY TRADE, SWING, or LEAP), direction, confidence (85-99), strike, expiration, thesis.
    """
    
    try:
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=4000,
            system=sys_prompt, messages=[{"role": "user", "content": "Generate JSON."}]
        )
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        clean_json = re.sub(r'```json\n|```', '', raw_text).strip()
        parsed_data = json.loads(clean_json)
        
        # Atomically write to the file the web server reads from
        with open("latest_scans.json", "w") as f:
            json.dump(parsed_data, f, indent=4)
        print(f"✅ Scan successful. latest_scans.json updated with {len(parsed_data.get('plays', []))} total setups.")
            
    except Exception as e:
        print(f"Anthropic / JSON Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_apex_scan())