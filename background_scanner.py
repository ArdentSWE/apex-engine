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

async def analyze_flow_and_chart(ticker: str):
    """
    PHASE 1: Finds the live price and charts the technicals.
    PHASE 2: Scans for Whale Flow.
    PHASE 3: Confirms the setup if Flow and Chart agree.
    """
    try:
        # -----------------------------------------------------
        # 1. READ THE CHART (Technicals)
        # -----------------------------------------------------
        df = await asyncio.to_thread(yf.download, tickers=ticker, period="5d", interval="5m", progress=False)
        if df.empty or len(df) < 20: return None
        
        if isinstance(df.columns, pd.MultiIndex): 
            df.columns = df.columns.droplevel(1)

        df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['Cum_Vol'] = df['Volume'].cumsum()
        df['Cum_Vol_Price'] = (df['Typical_Price'] * df['Volume']).cumsum()
        
        # Core Indicators
        df['VWAP'] = df['Cum_Vol_Price'] / df['Cum_Vol']
        df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()

        close = float(df['Close'].iloc[-1])
        vwap = float(df['VWAP'].iloc[-1])
        ema9 = float(df['EMA9'].iloc[-1])

        # -----------------------------------------------------
        # 2. FIND THE WHALE FLOW (Tape Reading)
        # -----------------------------------------------------
        if not POLYGON_KEY: return None

        day_calls, day_puts = 0, 0
        swing_calls, swing_puts = 0, 0
        leap_calls, leap_puts = 0, 0

        # Bound to +/- 20% to avoid dead strikes
        min_strike, max_strike = close * 0.80, close * 1.20
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
                            exp_str = details.get('expiration_date', '')

                            vol = c.get('day', {}).get('volume', 0)
                            if vol == 0: vol = c.get('open_interest', 0) # Fallback to OI
                            
                            vwap_price = c.get('day', {}).get('vwap', 0)
                            price = vwap_price if vwap_price > 0 else c.get('last_quote', {}).get('ask', 0)

                            if not exp_str or vol == 0 or price == 0: continue

                            prem = vol * price * 100
                            exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                            dte = (exp_date - datetime.date.today()).days

                            # Bucket by Play Type
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
                        await asyncio.sleep(5) # Prevent DDoS
                    else:
                        break

        # -----------------------------------------------------
        # 3. CONFIRM THE SETUP (Flow + Chart Convergence)
        # -----------------------------------------------------
        confirmed_plays = []
        metrics = {"close": close, "vwap": vwap, "ema9": ema9}

        # A. Evaluate Day Trades (0-7 DTE)
        total_day = day_calls + day_puts
        if total_day > 250000:
            call_skew = day_calls / total_day
            # Confirm Call Setup
            if call_skew > 0.60 and close > vwap:
                print(f"[{ticker}] ⚡ DAY CALLS Confirmed | Flow: ${day_calls/1000000:.1f}M | Chart: Price > VWAP")
                confirmed_plays.append({"ticker": ticker, "type": "DAY TRADE", "dir": "CALLS", "score": total_day * call_skew, "data": metrics})
            # Confirm Put Setup
            elif call_skew < 0.40 and close < vwap:
                print(f"[{ticker}] ⚡ DAY PUTS Confirmed | Flow: ${day_puts/1000000:.1f}M | Chart: Price < VWAP")
                confirmed_plays.append({"ticker": ticker, "type": "DAY TRADE", "dir": "PUTS", "score": total_day * (1 - call_skew), "data": metrics})

        # B. Evaluate Swings (8-90 DTE)
        total_swing = swing_calls + swing_puts
        if total_swing > 500000:
            call_skew = swing_calls / total_swing
            # Confirm Call Setup
            if call_skew > 0.60 and close > ema9:
                print(f"[{ticker}] 🎯 SWING CALLS Confirmed | Flow: ${swing_calls/1000000:.1f}M | Chart: Price > EMA9")
                confirmed_plays.append({"ticker": ticker, "type": "SWING", "dir": "CALLS", "score": total_swing * call_skew, "data": metrics})
            # Confirm Put Setup
            elif call_skew < 0.40 and close < ema9:
                print(f"[{ticker}] 🎯 SWING PUTS Confirmed | Flow: ${swing_puts/1000000:.1f}M | Chart: Price < EMA9")
                confirmed_plays.append({"ticker": ticker, "type": "SWING", "dir": "PUTS", "score": total_swing * (1 - call_skew), "data": metrics})

        # C. Evaluate Leaps (90+ DTE)
        total_leap = leap_calls + leap_puts
        if total_leap > 1000000:
            call_skew = leap_calls / total_leap
            # Confirm Call Setup
            if call_skew > 0.65 and close > ema9:
                print(f"[{ticker}] 🔭 LEAP CALLS Confirmed | Flow: ${leap_calls/1000000:.1f}M | Chart: Price > EMA9")
                confirmed_plays.append({"ticker": ticker, "type": "LEAP", "dir": "CALLS", "score": total_leap * call_skew, "data": metrics})
            # Confirm Put Setup
            elif call_skew < 0.35 and close < ema9:
                print(f"[{ticker}] 🔭 LEAP PUTS Confirmed | Flow: ${leap_puts/1000000:.1f}M | Chart: Price < EMA9")
                confirmed_plays.append({"ticker": ticker, "type": "LEAP", "dir": "PUTS", "score": total_leap * (1 - call_skew), "data": metrics})

        return confirmed_plays
        
    except Exception as e:
        return None

async def run_apex_scan():
    print(f"[{datetime.datetime.now()}] 🔍 INITIATING FLOW -> CHART SCANNER...\n")
    
    sem = asyncio.Semaphore(10)
    async def safe_scan(t):
        async with sem:
            await asyncio.sleep(0.2)
            return await analyze_flow_and_chart(t)
            
    results = await asyncio.gather(*[safe_scan(t) for t in TOP_100_TICKERS])
    
    # Flatten the results
    all_plays = []
    for r in results:
        if r: all_plays.extend(r)

    # Sort into categories
    day_plays = sorted([p for p in all_plays if p['type'] == "DAY TRADE"], key=lambda x: x['score'], reverse=True)[:6]
    swing_plays = sorted([p for p in all_plays if p['type'] == "SWING"], key=lambda x: x['score'], reverse=True)[:6]
    leap_plays = sorted([p for p in all_plays if p['type'] == "LEAP"], key=lambda x: x['score'], reverse=True)[:6]
    
    if not day_plays and not swing_plays and not leap_plays:
        print("\nScan complete. No setups found where Whale Flow and Chart Technicals agree.")
        with open("latest_scans.json", "w") as f: json.dump({"plays": []}, f)
        return

    print(f"\n✅ Scan Complete. Found {len(day_plays)} Day Trades, {len(swing_plays)} Swings, {len(leap_plays)} Leaps.")
    print("Formatting payload for website...")

    context_data = f"--- DAY TRADES ---\n{day_plays}\n--- SWINGS ---\n{swing_plays}\n--- LEAPS ---\n{leap_plays}"

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    sys_prompt = f"""
    You are the 'Apex Options Desk'. Convert these verified setups into the UI JSON payload.
    The technicals (Price, EMA9, VWAP) ALIGN perfectly with the directional options flow.
    DO NOT guess the direction, strictly use the "dir" (CALLS or PUTS) provided.
    
    In your thesis, briefly mention the flow and the chart confirmation (e.g. "Massive call flow detected as price breaks above VWAP").
    
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