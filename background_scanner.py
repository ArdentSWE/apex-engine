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
    "SHOP", "WDAY", "MDB", "UBER", "ROKU", "COIN", "PYPL", "HOOD", "MARA", "DKNG"
]

async def get_technical_trend(ticker: str):
    """STEP 1: Technical scan using Yahoo Finance."""
    try:
        df = await asyncio.to_thread(yf.download, tickers=ticker, period="5d", interval="5m", progress=False)
        if df.empty or len(df) < 20: return None
        
        if isinstance(df.columns, pd.MultiIndex): 
            df.columns = df.columns.droplevel(1)

        df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['Vol_Price'] = df['Typical_Price'] * df['Volume']
        df['Date'] = df.index.date
        df['Cum_Vol'] = df.groupby('Date')['Volume'].cumsum()
        df['Cum_Vol_Price'] = df.groupby('Date')['Vol_Price'].cumsum()
        df['VWAP'] = df['Cum_Vol_Price'] / df['Cum_Vol']
        df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()

        close = float(df['Close'].iloc[-1])
        ema9 = float(df['EMA9'].iloc[-1])
        vwap = float(df['VWAP'].iloc[-1])

        trend = "CHOP"
        if close > ema9 and ema9 > vwap: trend = "BULLISH"
        elif close < ema9 and ema9 < vwap: trend = "BEARISH"

        return {"ticker": ticker, "close": close, "ema9": ema9, "vwap": vwap, "trend": trend}
    except Exception as e:
        return None

async def get_options_flow(ticker_data):
    """STEP 2: Hits Polygon Options to verify institutional backing."""
    if not POLYGON_KEY: return None
    
    ticker = ticker_data['ticker']
    close = ticker_data['close']

    day_calls, day_puts = 0, 0
    swing_calls, swing_puts = 0, 0
    leap_calls, leap_puts = 0, 0

    try:
        # THE FIX: Tighten bounds to 8% and filter out expired ghost contracts
        min_strike, max_strike = close * 0.92, close * 1.08
        today_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&limit=250&apiKey={POLYGON_KEY}"
        pages = 0

        async with aiohttp.ClientSession() as session:
            while url and pages < 15:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("results", []):
                            details = c.get('details', {})
                            ctype = details.get('contract_type', '').lower()
                            exp_str = details.get('expiration_date', '')

                            vol = c.get('day', {}).get('volume', 0)
                            if vol == 0: vol = c.get('open_interest', 0) 
                            
                            vwap = c.get('day', {}).get('vwap', 0)
                            price = vwap if vwap > 0 else c.get('last_quote', {}).get('ask', 0)

                            if not exp_str or vol == 0 or price == 0: continue

                            prem = vol * price * 100
                            exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                            dte = (exp_date - datetime.date.today()).days

                            if dte <= 7:
                                if ctype == 'call': day_calls += prem
                                else: day_puts += prem
                            elif 8 <= dte <= 90:
                                if ctype == 'call': swing_calls += prem
                                else: swing_puts += prem
                            elif dte > 90:
                                if ctype == 'call': leap_calls += prem
                                else: leap_puts += prem

                        next_url = data.get("next_url")
                        url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                        pages += 1
                    elif resp.status == 429:
                        await asyncio.sleep(5) 
                    else:
                        break

        ticker_data.update({
            "day_calls": day_calls, "day_puts": day_puts,
            "swing_calls": swing_calls, "swing_puts": swing_puts,
            "leap_calls": leap_calls, "leap_puts": leap_puts
        })
        return ticker_data
        
    except Exception as e:
        return None

async def run_apex_scan():
    print(f"[{datetime.datetime.now()}] PHASE 1: TECHNICAL TREND SCAN...")
    
    tech_sem = asyncio.Semaphore(5)
    async def safe_tech(t):
        async with tech_sem:
            await asyncio.sleep(0.5) 
            return await get_technical_trend(t)
            
    tech_results = await asyncio.gather(*[safe_tech(t) for t in TOP_100_TICKERS])
    trending_tickers = [r for r in tech_results if r is not None and r['trend'] != "CHOP"]
    
    print(f"Found {len(trending_tickers)} tickers in confirmed mechanical trends. Discarding the chop.")
    if not trending_tickers:
        print("Market is completely flat. No setups found.")
        with open("latest_scans.json", "w") as f: json.dump({"plays": []}, f)
        return

    print(f"\nPHASE 2: CROSS-REFERENCING INSTITUTIONAL FLOW VIA POLYGON...")
    flow_sem = asyncio.Semaphore(10)
    async def safe_flow(data):
        async with flow_sem:
            await asyncio.sleep(0.2)
            return await get_options_flow(data)
            
    flow_results = await asyncio.gather(*[safe_flow(d) for d in trending_tickers])
    confirmed_data = [r for r in flow_results if r is not None]

    day_cands, swing_cands, leap_cands = [], [], []

    for r in confirmed_data:
        trend = r['trend']
        total_day = r['day_calls'] + r['day_puts']
        if total_day > 250000:
            call_skew = r['day_calls'] / total_day
            if trend == "BULLISH" and call_skew > 0.60:
                day_cands.append({"ticker": r['ticker'], "score": total_day * call_skew, "data": r, "dir": "CALLS"})
            elif trend == "BEARISH" and call_skew < 0.40:
                day_cands.append({"ticker": r['ticker'], "score": total_day * (1 - call_skew), "data": r, "dir": "PUTS"})

        total_swing = r['swing_calls'] + r['swing_puts']
        if total_swing > 500000:
            call_skew = r['swing_calls'] / total_swing if total_swing > 0 else 0
            if trend == "BULLISH" and call_skew > 0.60:
                swing_cands.append({"ticker": r['ticker'], "score": total_swing * call_skew, "data": r, "dir": "CALLS"})
            elif trend == "BEARISH" and call_skew < 0.40:
                swing_cands.append({"ticker": r['ticker'], "score": total_swing * (1 - call_skew), "data": r, "dir": "PUTS"})

        total_leap = r['leap_calls'] + r['leap_puts']
        if total_leap > 1000000:
            call_skew = r['leap_calls'] / total_leap if total_leap > 0 else 0
            if trend == "BULLISH" and call_skew > 0.60:
                leap_cands.append({"ticker": r['ticker'], "score": total_leap * call_skew, "data": r, "dir": "CALLS"})
            elif trend == "BEARISH" and call_skew < 0.40:
                leap_cands.append({"ticker": r['ticker'], "score": total_leap * (1 - call_skew), "data": r, "dir": "PUTS"})

    top_days = [{"ticker": c['ticker'], "dir": c['dir'], "metrics": c['data']} for c in sorted(day_cands, key=lambda x: x['score'], reverse=True)[:6]]
    top_swings = [{"ticker": c['ticker'], "dir": c['dir'], "metrics": c['data']} for c in sorted(swing_cands, key=lambda x: x['score'], reverse=True)[:6]]
    top_leaps = [{"ticker": c['ticker'], "dir": c['dir'], "metrics": c['data']} for c in sorted(leap_cands, key=lambda x: x['score'], reverse=True)[:6]]
    
    if not top_days and not top_swings and not top_leaps:
        print("\nScan complete. Tech is trending, but Flow isn't confirming. Writing empty schema.")
        with open("latest_scans.json", "w") as f: json.dump({"plays": []}, f)
        return

    print(f"\n[SCAN COMPLETE] Tech & Flow Converged on {len(top_days)} Day Trades, {len(top_swings)} Swings, and {len(top_leaps)} Whales. Generating Payload...")

    context_data = f"--- DAY TRADES ---\n{top_days}\n--- SWINGS ---\n{top_swings}\n--- LEAPS ---\n{top_leaps}"

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    sys_prompt = f"""
    You are the 'Apex Options Desk'. Convert these mathematically verified setups into the UI JSON payload.
    The technicals (Price, EMA9, VWAP) ALIGN perfectly with the directional options flow.
    DO NOT guess the direction, strictly use the "dir" (CALLS or PUTS) provided.
    
    In your thesis, briefly mention the flow and the chart confirmation.
    
    Raw Telemetry:
    {context_data}
    
    Return ONLY a JSON object with a "plays" array containing objects with: 
    ticker, play_type (DAY TRADE, SWING, or LEAP), direction (CALLS or PUTS), confidence (88-99), strike, expiration, thesis.
    """
    
    try:
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=4000,
            system=sys_prompt, messages=[{"role": "user", "content": "Generate JSON."}]
        )
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            parsed_data = json.loads(match.group())
            with open("latest_scans.json", "w") as f:
                json.dump(parsed_data, f, indent=4)
            print(f"✅ Mission Accomplished. latest_scans.json populated with {len(parsed_data.get('plays', []))} validated plays.")
        else:
            print("Failed to parse JSON.")
    except Exception as e:
        print(f"Anthropic / JSON Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_apex_scan())