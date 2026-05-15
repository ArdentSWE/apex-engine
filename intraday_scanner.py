import os
import asyncio
import datetime
import pytz
import json
import re
import aiohttp
import pandas as pd
from anthropic import AsyncAnthropic

POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# 🚀 ALPACA HFT DATA KEYS
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "PKQ5GXEMLUIH5D7W2XVVTSLS5O")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "WfunuBNhpLv7JqiPQVKfxvfAUJZDYzDhoa3DSEbRLVV")

# Focused watchlist for 0DTE / High-Velocity Scalping
SCALP_WATCHLIST = [
    "SPY", "QQQ", "IWM", "NVDA", "TSLA", "AMD", "AAPL", "META", "AMZN", "MSFT",
    "NFLX", "SMCI", "AVGO", "COIN", "MSTR", "PLTR", "ARM", "MU", "QCOM", "BA"
]

async def get_bulk_alpaca_technicals():
    """STEP 1: Ultra-fast, single-call bulk request to Alpaca's API."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("❌ Missing Alpaca API Keys. Cannot fetch technicals.")
        return []

    try:
        symbols = ",".join(SCALP_WATCHLIST)
        # Limit 500 gives us ~1.5 days of 5m candles, plenty for EMA9 and Daily VWAP
        url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols}&timeframe=5Min&limit=500&feed=iex"
        
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY.strip(),
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY.strip()
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    print(f"Alpaca API Error: {err}")
                    return []
                data = await resp.json()
                bars = data.get("bars", {})

        trending_tickers = []

        # Process all tickers instantly in memory
        for ticker, ticker_bars in bars.items():
            if not ticker_bars or len(ticker_bars) < 20: continue

            df = pd.DataFrame(ticker_bars)
            df['Time'] = pd.to_datetime(df['t'])
            df = df.set_index('Time').tz_convert('America/New_York')

            df['Typical_Price'] = (df['h'] + df['l'] + df['c']) / 3
            df['Vol_Price'] = df['Typical_Price'] * df['v']
            df['Date'] = df.index.date
            
            # Daily VWAP Reset
            df['Cum_Vol'] = df.groupby('Date')['v'].cumsum()
            df['Cum_Vol_Price'] = df.groupby('Date')['Vol_Price'].cumsum()
            df['VWAP'] = df['Cum_Vol_Price'] / df['Cum_Vol']
            df['EMA9'] = df['c'].ewm(span=9, adjust=False).mean()

            close_px = float(df['c'].iloc[-1])
            ema9 = float(df['EMA9'].iloc[-1])
            vwap = float(df['VWAP'].iloc[-1])

            # INTRADAY TREND DEFINITION
            trend = "CHOP"
            if close_px > ema9 and ema9 > vwap: trend = "BULLISH"
            elif close_px < ema9 and ema9 < vwap: trend = "BEARISH"

            if trend != "CHOP":
                trending_tickers.append({
                    "ticker": ticker, 
                    "close": close_px, 
                    "trend": trend
                })

        return trending_tickers

    except Exception as e:
        print(f"Bulk Alpaca Processing Error: {e}")
        return []

async def get_0dte_flow(ticker_data):
    """STEP 2: Hits Polygon strictly for 0DTE to 7DTE options flow."""
    if not POLYGON_KEY: return None
    
    ticker = ticker_data['ticker']
    close_px = ticker_data['close']

    day_calls, day_puts = 0, 0

    try:
        # TIGHT BOUNDS: +/- 5% Strike range
        min_strike, max_strike = close_px * 0.95, close_px * 1.05
        now_pt = datetime.datetime.now(pytz.timezone('America/New_York'))
        today_str = now_pt.strftime('%Y-%m-%d')
        week_out_str = (now_pt + datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        
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

        ticker_data.update({"day_calls": day_calls, "day_puts": day_puts})
        return ticker_data
        
    except Exception as e:
        print(f"Polygon Error on {ticker}: {e}")
        return None

async def run_intraday_scan():
    print(f"[{datetime.datetime.now()}] ⚡ INITIATING 0DTE/WEEKLY SCALP SCAN...")
    
    # PHASE 1: BULK ALPACA FETCH
    print("📡 Fetching bulk IEX data from Alpaca...")
    trending_tickers = await get_bulk_alpaca_technicals()
    
    if not trending_tickers:
        print("Market is chopping. No Intraday setups found.")
        return
        
    print(f"✅ Found {len(trending_tickers)} tickers in confirmed intraday trends.")

    # PHASE 2: POLYGON FLOW VERIFICATION
    print(f"\n📡 CROSS-REFERENCING TICKERS WITH 0DTE FLOW...")
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
                candidates.append({"ticker": t, "play_type": "0DTE SCALP", "direction": "CALLS", "flow": total_day, "skew": call_skew, "trend": trend})
            elif trend == "BEARISH" and call_skew < 0.40:
                print(f"[{t}] 0DTE PUTS | Flow: ${total_day/1000000:.1f}M | Skew: {(1-call_skew)*100:.1f}%")
                candidates.append({"ticker": t, "play_type": "0DTE SCALP", "direction": "PUTS", "flow": total_day, "skew": 1-call_skew, "trend": trend})

    if not candidates:
        print("\nFlow not confirming. Aborting.")
        return

    # Select the absolute best setup
    candidates.sort(key=lambda x: x['flow'] * x['skew'], reverse=True)
    best_plays = candidates[:3]

    market_context = []
    for c in best_plays:
        market_context.append(f"[{c['ticker']}] Type: {c['play_type']} | Trend: {c['trend']} | Flow: ${c['flow']/1000000:.1f}M | Skew: {c['skew']*100:.1f}% {c['direction']}")

    print(f"\n🧠 PASSING TO APEX QUANT ENGINE...")
    
    # PHASE 4: THE APEX MASTER PROMPT
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
    1. STRIKE PRICE: You MUST output "ATM" (At-The-Money) or "First OTM". Do not invent a specific dollar strike.
    2. EXPIRATION: You MUST output "0DTE" (for Day Trades) or "Front-Week" (for Swings). Do not guess the exact calendar date.
    3. SL & TP: Do NOT invent arbitrary dollar amounts for entry or exit. You MUST use algorithmic or percentage-based risk management rules (e.g., "SL: -20% or 5m close below VWAP").

    [YOUR DIRECTIVE]
    Output exactly ONE high-probability trade idea based on the provided telemetry. 
    Format your response STRICTLY as a JSON object with the following schema:

    {{
        "ticker": "String (e.g., SPY)",
        "play_type": "String (Must be '0DTE SCALP' or 'WEEKLY SWING')",
        "trend": "String (e.g., BULLISH or BEARISH)",
        "direction": "String (CALLS or PUTS)",
        "confidence": 95,
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
        
        with open("latest_0dte_scans.json", "w") as f:
            json.dump({"plays": [parsed_data]}, f, indent=4)
            
        print(f"✅ Mission Accomplished. Intraday payload secured for {parsed_data.get('ticker')}.")
    except Exception as e:
        print(f"Anthropic / JSON Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_intraday_scan())