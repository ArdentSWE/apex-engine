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
import logging

# Mute yfinance background noise
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# Focused watchlist for 0DTE / High-Velocity Scalping
SCALP_WATCHLIST = [
    "SPY", "QQQ", "IWM", "NVDA", "TSLA", "AMD", "AAPL", "META", "AMZN", "MSFT",
    "NFLX", "SMCI", "AVGO", "COIN", "MSTR", "PLTR", "ARM", "MU", "QCOM", "BA"
]

async def get_intraday_technicals(ticker: str):
    """STEP 1: Fast Intraday Technical scan using Yahoo Finance."""
    try:
        df = await asyncio.to_thread(yf.download, tickers=ticker, period="2d", interval="5m", progress=False)
        if df.empty or len(df) < 10: 
            return None
        
        if isinstance(df.columns, pd.MultiIndex): 
            df.columns = df.columns.droplevel(1)

        # Daily VWAP Calculation
        df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['Vol_Price'] = df['Typical_Price'] * df['Volume']
        df['Date'] = df.index.date
        df['Cum_Vol'] = df.groupby('Date')['Volume'].cumsum()
        df['Cum_Vol_Price'] = df.groupby('Date')['Vol_Price'].cumsum()
        df['VWAP'] = df['Cum_Vol_Price'] / df['Cum_Vol']
        df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()

        close_px = float(df['Close'].iloc[-1])
        ema9 = float(df['EMA9'].iloc[-1])
        vwap = float(df['VWAP'].iloc[-1])

        # INTRADAY TREND DEFINITION
        trend = "CHOP"
        if close_px > ema9 and ema9 > vwap: trend = "BULLISH"
        elif close_px < ema9 and ema9 < vwap: trend = "BEARISH"

        return {"ticker": ticker, "close": close_px, "ema9": ema9, "vwap": vwap, "trend": trend}
    except Exception as e:
        return None

async def get_0dte_flow(ticker_data):
    """STEP 2: Hits Polygon strictly for 0DTE to 7DTE options flow."""
    if not POLYGON_KEY: return None
    
    ticker = ticker_data['ticker']
    close_px = ticker_data['close']

    day_calls, day_puts = 0, 0

    try:
        # TIGHT BOUNDS: +/- 5% Strike range to capture only ATM/Near-OTM flow
        min_strike, max_strike = close_px * 0.95, close_px * 1.05
        
        now_pt = datetime.datetime.now(pytz.timezone('America/New_York'))
        today_str = now_pt.strftime('%Y-%m-%d')
        week_out_str = (now_pt + datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        
        # POLGYON API FILTER: Only fetch contracts expiring between today and +7 days
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&expiration_date.lte={week_out_str}&limit=250&apiKey={POLYGON_KEY}"
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
                            if vol == 0: vol = c.get('open_interest', 0) 
                            
                            vwap = c.get('day', {}).get('vwap', 0)
                            price = vwap if vwap > 0 else c.get('last_quote', {}).get('ask', 0)

                            if not exp_str or vol == 0 or price == 0: continue

                            prem = vol * price * 100
                            exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                            dte = (exp_date - now_pt.date()).days

                            # Strict 0-7 DTE bucket
                            if 0 <= dte <= 7:
                                if ctype == 'call': day_calls += prem
                                else: day_puts += prem

                        next_url = data.get("next_url")
                        url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                        pages += 1
                    elif resp.status == 429:
                        await asyncio.sleep(5) 
                    else:
                        break

        ticker_data.update({
            "day_calls": day_calls, 
            "day_puts": day_puts,
        })
        return ticker_data
        
    except Exception as e:
        print(f"Polygon Error on {ticker}: {e}")
        return None

async def run_intraday_scan():
    print(f"[{datetime.datetime.now()}] ⚡ INITIATING 0DTE/WEEKLY SCALP SCAN...")
    
    # PHASE 1
    tech_sem = asyncio.Semaphore(5)
    async def safe_tech(t):
        async with tech_sem:
            await asyncio.sleep(0.3) 
            return await get_intraday_technicals(t)
            
    tech_results = await asyncio.gather(*[safe_tech(t) for t in SCALP_WATCHLIST])
    trending_tickers = [r for r in tech_results if r is not None and r['trend'] != "CHOP"]
    
    if not trending_tickers:
        print("Market is chopping. No Intraday setups found.")
        return

    # PHASE 2
    print(f"\n📡 CROSS-REFERENCING {len(trending_tickers)} TICKERS WITH 0DTE FLOW...")
    flow_sem = asyncio.Semaphore(10)
    async def safe_flow(data):
        async with flow_sem:
            await asyncio.sleep(0.2)
            return await get_0dte_flow(data)
            
    flow_results = await asyncio.gather(*[safe_flow(d) for d in trending_tickers])
    confirmed_data = [r for r in flow_results if r is not None]

    candidates = []

    # PHASE 3: CONVERGENCE FILTER
    for r in confirmed_data:
        trend = r['trend']
        t = r['ticker']
        total_day = r['day_calls'] + r['day_puts']
        
        if total_day > 250000:
            call_skew = r['day_calls'] / total_day
            if trend == "BULLISH" and call_skew > 0.60:
                print(f"[{t}] 0DTE CALLS | Flow: ${total_day/1000000:.1f}M | Skew: {call_skew*100:.1f}%")
                candidates.append({"ticker": t, "type": "0DTE SCALP", "dir": "CALLS", "flow": total_day, "skew": call_skew, "trend": trend})
            elif trend == "BEARISH" and call_skew < 0.40:
                print(f"[{t}] 0DTE PUTS | Flow: ${total_day/1000000:.1f}M | Skew: {(1-call_skew)*100:.1f}%")
                candidates.append({"ticker": t, "type": "0DTE SCALP", "dir": "PUTS", "flow": total_day, "skew": 1-call_skew, "trend": trend})

    if not candidates:
        print("\nFlow not confirming. Aborting.")
        return

    # Select the absolute best setup
    candidates.sort(key=lambda x: x['flow'] * x['skew'], reverse=True)
    best_plays = candidates[:3]

    market_context = []
    for c in best_plays:
        market_context.append(f"[{c['ticker']}] Type: {c['type']} | Trend: {c['trend']} | Flow: ${c['flow']/1000000:.1f}M | Skew: {c['skew']*100:.1f}% {c['dir']}")

    print(f"\n🧠 PASSING TO APEX QUANT ENGINE...")
    
    # -----------------------------------------------------
    # PHASE 4: THE APEX MASTER PROMPT (ANTI-HALLUCINATION)
    # -----------------------------------------------------
    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    now_str = datetime.datetime.now(pytz.timezone('America/Los_Angeles')).strftime('%Y-%m-%d %I:%M %p PT')

    sys_prompt = f"""
    You are the 'Apex Quant Engine', an elite autonomous AI operating the 0DTE and Weekly options desk for the Ace's House Syndicate.

    Your objective is to ingest raw institutional telemetry and convert it into a highly structured, executable trade payload.

    [RAW TELEMETRY DATA]
    {chr(10).join(market_context)}

    CURRENT DATE & TIME: {now_str}

    🚨 CRITICAL ANTI-HALLUCINATION PROTOCOL 🚨
    You are strictly forbidden from inventing, guessing, or hallucinating financial data. You do not have access to live option chain pricing. 
    1. STRIKE PRICE: You MUST output "ATM" (At-The-Money) or "First OTM". Do not invent a specific dollar strike (e.g., $155C).
    2. EXPIRATION: You MUST output "0DTE" (for Day Trades) or "Front-Week" (for Swings). Do not guess the exact calendar date.
    3. SL & TP: Do NOT invent arbitrary dollar amounts for entry or exit (e.g., "Enter at $1.50, Stop at $1.20"). You MUST use algorithmic or percentage-based risk management rules (e.g., "SL: -20% or 5m close below VWAP").

    [YOUR DIRECTIVE]
    Output exactly ONE high-probability trade idea based on the provided telemetry. 
    Format your response STRICTLY as a JSON object with the following schema:

    {{
        "ticker": "String (e.g., SPY)",
        "play_type": "String (Must be '0DTE SCALP' or 'WEEKLY SWING')",
        "trend": "String (e.g., BULLISH or BEARISH)",
        "direction": "String (CALLS or PUTS)",
        "confidence": "Integer (80-99)",
        "strike": "String (Strictly 'ATM' or 'First OTM')",
        "expiration": "String (Strictly '0DTE' or 'Front-Week')",
        "sl": "String (e.g., '-20% Premium or Trend Invalidation')",
        "tp": "String (e.g., '+30% / Scale on Momentum')",
        "thesis": "String (Write a ruthless, 2-sentence institutional breakdown. Explicitly mention the flow skew and the technical setup. End the thesis with: '♠️ Ace's House Quant Desk'.)"
    }}

    Respond ONLY with the raw JSON object. Do not include markdown formatting like ```json.
    """

    try:
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=1000,
            system=sys_prompt, messages=[{"role": "user", "content": "Execute Intraday Payload."}]
        )
        
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        clean_json = re.sub(r'```json\n|```', '', raw_text).strip()
        if "{" in clean_json: clean_json = clean_json[clean_json.find("{"):clean_json.rfind("}")+1]
        
        parsed_data = json.loads(clean_json)
        
        # Save isolated 0DTE scans (so they don't overwrite Swings/Leaps)
        with open("latest_0dte_scans.json", "w") as f:
            json.dump({"plays": [parsed_data]}, f, indent=4)
            
        print(f"✅ Mission Accomplished. Intraday payload secured for {parsed_data.get('ticker')}.")
    except Exception as e:
        print(f"Anthropic / JSON Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_intraday_scan())