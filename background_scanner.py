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

TOP_100_TICKERS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD", "META", "AMZN", "GOOGL",
    "AVGO", "NFLX", "SMCI", "COST", "JPM", "WMT", "V", "MA", "XOM", "UNH",
    "JNJ", "PG", "HD", "ORCL", "CRM", "BAC", "ABBV", "CVX", "MRK", "KO",
    "CSCO", "IBM", "LIN", "ASML", "PEP", "TMO", "NOW", "DIS", "MCD", "INTC",
    "INTU", "QCOM", "TXN", "AMAT", "MU", "PANW", "ADI", "KLAC", "LRCX", "SNPS",
    "CRWD", "FTNT", "PLTR", "SNOW", "ZS", "DDOG", "NET", "TEAM", "SHOP", "WDAY",
    "MDB", "UBER", "ROKU", "COIN", "PYPL", "HOOD", "MARA", "DKNG", "BA", "CAT"
]

async def analyze_ticker(ticker: str, session: aiohttp.ClientSession):
    """Fetches Technicals, News, and Swing/LEAP Options Flow in one pass."""
    try:
        # -----------------------------------------------------
        # 1. TECHNICALS & NEWS (Yahoo Finance)
        # -----------------------------------------------------
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        df = await asyncio.to_thread(stock.history, period="5d", interval="5m")
        
        trend = "CHOP"
        close_px = 0.0
        news_context = "No recent catalysts."
        
        if not df.empty and len(df) >= 20:
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

            if close_px > ema9 and ema9 > vwap: trend = "BULLISH"
            elif close_px < ema9 and ema9 < vwap: trend = "BEARISH"
            
            news_items = stock.news
            if news_items:
                headlines = [n.get('title') for n in news_items[:2] if n.get('title')]
                if headlines:
                    news_context = " | ".join(headlines)

        if close_px == 0: return None

        # -----------------------------------------------------
        # 2. SWING & LEAP OPTIONS FLOW (Polygon)
        # -----------------------------------------------------
        swing_calls, swing_puts = 0, 0
        leap_calls, leap_puts = 0, 0

        min_strike, max_strike = close_px * 0.85, close_px * 1.15
        
        # EXCLUDE 0DTEs/WEEKLIES: Only pull contracts 8+ days out
        now_pt = datetime.datetime.now(pytz.timezone('America/New_York'))
        swing_start_date = (now_pt + datetime.timedelta(days=8)).strftime('%Y-%m-%d')
        
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={swing_start_date}&limit=250&apiKey={POLYGON_KEY}"
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

        return {
            "ticker": ticker, "close": close_px, "trend": trend, "news": news_context,
            "swing_calls": swing_calls, "swing_puts": swing_puts,
            "leap_calls": leap_calls, "leap_puts": leap_puts
        }
    except Exception as e:
        return None

async def run_apex_scan():
    print(f"[{datetime.datetime.now()}] 🔍 INITIATING SWING & LEAP MATIX SCAN...")
    
    sem = asyncio.Semaphore(5) # Protect API limits
    
    async def safe_analyze(ticker, session):
        async with sem:
            await asyncio.sleep(0.3)
            return await analyze_ticker(ticker, session)

    async with aiohttp.ClientSession() as session:
        tasks = [safe_analyze(t, session) for t in TOP_100_TICKERS]
        results = await asyncio.gather(*tasks)

    valid_data = [r for r in results if r is not None]
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
                    "ticker": t, "type": "SWING", "dir": dir_str, "flow": total_swing, 
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
                logic_flag = "🐋 Heavy Institutional LEAP Accumulation"
            elif trend == "BULLISH" and dir_str == "CALLS" and strength > 0.60:
                logic_flag = "📈 Long-Term Tech Breakout Confirmed"
            elif trend == "BEARISH" and dir_str == "PUTS" and strength > 0.60:
                logic_flag = "📉 Long-Term Tech Breakdown Confirmed"

            if logic_flag:
                print(f"[{t}] LEAP {dir_str} | Flow: ${total_leap/1000000:.1f}M | Skew: {strength*100:.1f}% | Logic: {logic_flag}")
                candidates.append({
                    "ticker": t, "type": "LEAP", "dir": dir_str, "flow": total_leap, 
                    "skew": strength, "logic": logic_flag, "news": news
                })

    if not candidates:
        print("\nScan complete. Market is flat. Writing empty schema.")
        with open("latest_scans.json", "w") as f: json.dump({"plays": []}, f)
        return

    # Sort and pick the absolute best setups
    candidates.sort(key=lambda x: x['flow'] * x['skew'], reverse=True)
    top_plays = candidates[:15] # Send top 15 to the site

    print(f"\n✅ Scan Complete. Found {len(top_plays)} Premium Swing/LEAP Setups. Handing to Apex AI...")

    market_context = []
    for c in top_plays:
        market_context.append(f"[{c['ticker']}] {c['type']} {c['dir']} | Skew: {c['skew']*100:.1f}% | Catalyst: {c['logic']} | News: {c['news']}")

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    
    sys_prompt = f"""
    You are the 'Apex Options Desk', an autonomous quantitative AI.
    Convert these mathematically verified Swings and LEAPs into the UI JSON payload.
    
    Raw Telemetry:
    {chr(10).join(market_context)}
    
    CRITICAL ANTI-HALLUCINATION PROTOCOL: You do not have live option chain pricing. 
    You MUST set "strike" to "ATM".
    You MUST set "expiration" to "Next-Month" (for SWING) or "Back-Month" (for LEAP).
    
    In the "thesis", explicitly state WHY the play triggered (Whale Flow, Tech Breakout, or News Catalyst) using the provided data.
    
    Return ONLY a JSON object with a "plays" array containing objects with: 
    ticker, play_type (SWING or LEAP), direction (CALLS or PUTS), confidence (88-99), strike, expiration, thesis.
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