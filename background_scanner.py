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
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# 🚀 ALPACA HFT DATA KEYS
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "PKQ5GXEMLUIH5D7W2XVVTSLS5O")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "WfunuBNhpLv7JqiPQVKfxvfAUJZDYzDhoa3DSEbRLVV")

TOP_100_TICKERS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD", "META", "AMZN", "GOOGL",
    "AVGO", "NFLX", "SMCI", "COST", "JPM", "WMT", "V", "MA", "XOM", "UNH",
    "JNJ", "PG", "HD", "ORCL", "CRM", "BAC", "ABBV", "CVX", "MRK", "KO",
    "CSCO", "IBM", "LIN", "ASML", "PEP", "TMO", "NOW", "DIS", "MCD", "INTC",
    "INTU", "QCOM", "TXN", "AMAT", "MU", "PANW", "ADI", "KLAC", "LRCX", "SNPS",
    "CRWD", "FTNT", "PLTR", "SNOW", "ZS", "DDOG", "NET", "TEAM", "SHOP", "WDAY",
    "MDB", "UBER", "ROKU", "COIN", "PYPL", "HOOD", "MARA", "DKNG", "BA", "CAT"
]

async def get_bulk_alpaca_technicals():
    """STEP 1: Fast Bulk Request to Alpaca for Top 100 Technicals."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY: return []

    try:
        symbols = ",".join(TOP_100_TICKERS)
        url = f"[https://data.alpaca.markets/v2/stocks/bars?symbols=](https://data.alpaca.markets/v2/stocks/bars?symbols=){symbols}&timeframe=5Min&limit=500&feed=iex"
        headers = {"APCA-API-KEY-ID": ALPACA_API_KEY.strip(), "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY.strip()}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200: return []
                data = await resp.json()
                bars = data.get("bars", {})

        trending_tickers = []
        for ticker, ticker_bars in bars.items():
            if not ticker_bars or len(ticker_bars) < 20: continue

            df = pd.DataFrame(ticker_bars)
            df['Time'] = pd.to_datetime(df['t'])
            df = df.set_index('Time').tz_convert('America/New_York')

            df['Typical_Price'] = (df['h'] + df['l'] + df['c']) / 3
            df['Vol_Price'] = df['Typical_Price'] * df['v']
            df['Date'] = df.index.date
            
            df['Cum_Vol'] = df.groupby('Date')['v'].cumsum()
            df['Cum_Vol_Price'] = df.groupby('Date')['Vol_Price'].cumsum()
            df['VWAP'] = df['Cum_Vol_Price'] / df['Cum_Vol']
            df['EMA9'] = df['c'].ewm(span=9, adjust=False).mean()

            close_px = float(df['c'].iloc[-1])
            ema9 = float(df['EMA9'].iloc[-1])
            vwap = float(df['VWAP'].iloc[-1])

            trend = "CHOP"
            if close_px > ema9 and ema9 > vwap: trend = "BULLISH"
            elif close_px < ema9 and ema9 < vwap: trend = "BEARISH"

            if trend != "CHOP":
                trending_tickers.append({"ticker": ticker, "close": close_px, "trend": trend})

        return trending_tickers
    except Exception as e:
        print(f"Alpaca Error: {e}")
        return []

async def analyze_catalysts_and_flow(ticker_data, session: aiohttp.ClientSession):
    """STEP 2: For trending tickers, grab fresh news & 8+ DTE Flow."""
    ticker = ticker_data['ticker']
    close_px = ticker_data['close']
    
    # Grab News
    news_context = "No recent catalysts."
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        news_items = stock.news
        if news_items:
            headlines = [n.get('title') for n in news_items[:2] if n.get('title')]
            if headlines: news_context = " | ".join(headlines)
    except: pass

    # Grab Options Flow (8+ DTE)
    swing_calls, swing_puts = 0, 0
    leap_calls, leap_puts = 0, 0

    if POLYGON_KEY:
        try:
            min_strike, max_strike = close_px * 0.85, close_px * 1.15
            now_pt = datetime.datetime.now(pytz.timezone('America/New_York'))
            swing_start_date = (now_pt + datetime.timedelta(days=8)).strftime('%Y-%m-%d')
            
            url = f"[https://api.polygon.io/v3/snapshot/options/](https://api.polygon.io/v3/snapshot/options/){ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={swing_start_date}&limit=250&apiKey={POLYGON_KEY}"
            pages = 0
            
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
                            
                            vwap_price = c.get('day', {}).get('vwap', 0)
                            price = vwap_price if vwap_price > 0 else c.get('last_quote', {}).get('ask', 0)

                            if not exp_str or vol == 0 or price == 0: continue

                            prem = vol * price * 100
                            exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                            dte = (exp_date - now_pt.date()).days

                            if 8 <= dte <= 90:
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
        except Exception: pass

    ticker_data.update({
        "news": news_context,
        "swing_calls": swing_calls, "swing_puts": swing_puts,
        "leap_calls": leap_calls, "leap_puts": leap_puts
    })
    return ticker_data

async def run_apex_scan():
    print(f"[{datetime.datetime.now()}] 🔍 INITIATING SWING & LEAP MATIX SCAN...")
    
    print("📡 Fetching bulk IEX data from Alpaca for Top 100 Matrix...")
    trending_tickers = await get_bulk_alpaca_technicals()
    
    if not trending_tickers:
        print("Market is flat. Writing empty schema.")
        with open("latest_scans.json", "w") as f: json.dump({"plays": []}, f)
        return

    print(f"✅ Found {len(trending_tickers)} tickers in trend. Fetching Catalysts & Flow...")

    sem = asyncio.Semaphore(10)
    async def safe_analyze(data, session):
        async with sem:
            await asyncio.sleep(0.2)
            return await analyze_catalysts_and_flow(data, session)

    async with aiohttp.ClientSession() as session:
        tasks = [safe_analyze(d, session) for d in trending_tickers]
        valid_data = await asyncio.gather(*tasks)

    candidates = []
    print("\n--- 🧠 EVALUATING INSTITUTIONAL LOGIC ---")

    for r in valid_data:
        t = r['ticker']
        trend = r['trend']
        news = r['news']
        
        # SWINGS
        total_swing = r['swing_calls'] + r['swing_puts']
        if total_swing > 500000:
            call_skew = r['swing_calls'] / total_swing
            dir_str = "CALLS" if call_skew > 0.50 else "PUTS"
            strength = call_skew if dir_str == "CALLS" else (1 - call_skew)
            
            logic_flag = None
            if total_swing > 2500000 and strength > 0.70:
                logic_flag = "🐋 Heavy Whale Accumulation"
            elif trend == "BULLISH" and dir_str == "CALLS" and strength > 0.60:
                logic_flag = "📈 Perfect Tech + Flow Breakout"
            elif trend == "BEARISH" and dir_str == "PUTS" and strength > 0.60:
                logic_flag = "📉 Perfect Tech + Flow Breakdown"
            elif news != "No recent catalysts." and strength > 0.65:
                logic_flag = "📰 News Catalyst + Correlated Flow"

            if logic_flag:
                print(f"[{t}] SWING {dir_str} | Flow: ${total_swing/1000000:.1f}M | Skew: {strength*100:.1f}% | Logic: {logic_flag}")
                candidates.append({
                    "ticker": t, "play_type": "SWING", "direction": dir_str, "flow": total_swing, 
                    "skew": strength, "logic": logic_flag, "news": news
                })

        # LEAPS
        total_leap = r['leap_calls'] + r['leap_puts']
        if total_leap > 1000000:
            call_skew = r['leap_calls'] / total_leap
            dir_str = "CALLS" if call_skew > 0.50 else "PUTS"
            strength = call_skew if dir_str == "CALLS" else (1 - call_skew)
            
            logic_flag = None
            if total_leap > 3000000 and strength > 0.70:
                logic_flag = "🐋 Institutional LEAP Accumulation"
            elif trend == "BULLISH" and dir_str == "CALLS" and strength > 0.60:
                logic_flag = "📈 Long-Term Breakout Confirmed"
            elif trend == "BEARISH" and dir_str == "PUTS" and strength > 0.60:
                logic_flag = "📉 Long-Term Breakdown Confirmed"

            if logic_flag:
                print(f"[{t}] LEAP {dir_str} | Flow: ${total_leap/1000000:.1f}M | Skew: {strength*100:.1f}% | Logic: {logic_flag}")
                candidates.append({
                    "ticker": t, "play_type": "LEAP", "direction": dir_str, "flow": total_leap, 
                    "skew": strength, "logic": logic_flag, "news": news
                })

    if not candidates:
        print("\nScan complete. Market is flat. Writing empty schema.")
        with open("latest_scans.json", "w") as f: json.dump({"plays": []}, f)
        return

    candidates.sort(key=lambda x: x['flow'] * x['skew'], reverse=True)
    top_plays = candidates[:15]

    market_context = []
    for c in top_plays:
        market_context.append(f"[{c['ticker']}] Type: {c['play_type']} | Dir: {c['direction']} | Skew: {c['skew']*100:.1f}% | Catalyst: {c['logic']} | News: {c['news']}")

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    now_str = datetime.datetime.now(pytz.timezone('America/Los_Angeles')).strftime('%Y-%m-%d %I:%M %p PT')
    
    sys_prompt = f"""
    You are the 'Apex Options Desk', an autonomous quantitative AI for the Ace's House Syndicate.
    Convert these mathematically verified Swings and LEAPs into the UI JSON payload.
    
    Raw Telemetry:
    {chr(10).join(market_context)}
    
    CURRENT DATE & TIME: {now_str}

    🚨 CRITICAL ANTI-HALLUCINATION PROTOCOL 🚨
    You are strictly forbidden from inventing, guessing, or hallucinating financial data. You do not have access to live option chain pricing. 
    1. STRIKE PRICE: You MUST output "ATM" or "First OTM". Do not invent a specific dollar strike.
    2. EXPIRATION: You MUST output "Next-Month" (for SWINGS) or "Back-Month" (for LEAPS). Do not guess calendar dates.
    3. SL & TP: You MUST use algorithmic or percentage-based rules (e.g., "SL: Trend Invalidation", "TP: Scale on Momentum"). Do NOT invent arbitrary dollar amounts.

    In the "thesis", explicitly state WHY the play triggered (Whale Flow, Tech Breakout, or News Catalyst) using the provided data.
    
    Return ONLY a JSON object with a "plays" array containing objects with: 
    ticker, play_type (SWING or LEAP), direction (CALLS or PUTS), confidence (88-99), strike, expiration, sl, tp, thesis.
    """
    
    try:
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=4000,
            system=sys_prompt, messages=[{"role": "user", "content": "Generate JSON Payload."}]
        )
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        clean_json = re.sub(r'```json\n|```', '', raw_text).strip()
        if "{" in clean_json: clean_json = clean_json[clean_json.find("{"):clean_json.rfind("}")+1]
        
        parsed_data = json.loads(clean_json)
        with open("latest_scans.json", "w") as f:
            json.dump(parsed_data, f, indent=4)
        print(f"✅ Mission Accomplished. latest_scans.json populated with {len(parsed_data.get('plays', []))} elite plays.")
    except Exception as e:
        print(f"Anthropic / JSON Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_apex_scan())