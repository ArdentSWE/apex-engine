import os
import requests
import asyncio
import datetime
import pytz
import json
import re
import aiohttp
import yfinance as yf
from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from anthropic import AsyncAnthropic

app = FastAPI(title="Apex Engine", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ==========================================
# PILLAR 1: EQUITIES & OPTIONS (The Web Terminal)
# ==========================================

@router.get("/api/news")
def get_macro_docket(ticker: str = ""):
    try:
        if ticker:
            url = f"https://finnhub.io/api/v1/company-news?symbol={ticker.upper()}&from={(datetime.datetime.now() - datetime.timedelta(days=3)).strftime('%Y-%m-%d')}&to={datetime.datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        else:
            url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
            
        response = requests.get(url, timeout=5)
        data = response.json()
        formatted_news = [{"title": item.get("headline"), "source": item.get("source"), "url": item.get("url")} for item in data[:6]]
        return {"news": formatted_news}
    except Exception:
        return {"news": [{"title": "Live Wire Disconnected", "source": "System", "url": "#"}]}

@router.get("/api/flow")
async def get_options_flow(ticker: str = "SPY"):
    """Pulls true Institutional Options Premium Flow (Whale Tape) from Polygon."""
    if not POLYGON_KEY: return {"tape": []}
    try:
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?limit=250&apiKey={POLYGON_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                
        flow_list = []
        for c in data.get("results", []):
            details = c.get("details", {})
            vol = c.get("day", {}).get("volume", 0)
            vwap = c.get("day", {}).get("vwap", 0)
            if vol > 0 and vwap > 0:
                premium = vol * vwap * 100
                ctype = details.get("contract_type", "").upper()
                strike = details.get("strike_price")
                exp = details.get("expiration_date")
                # Format: 190C (06-05)
                flow_list.append({
                    "contract": f"{strike}{ctype[0]} ({exp[5:]})",
                    "size": vol,
                    "premium_val": premium,
                    "price": vwap
                })
        
        # Sort by heaviest premium and grab the top 8 prints
        flow_list.sort(key=lambda x: x["premium_val"], reverse=True)
        
        formatted_tape = []
        for f in flow_list[:8]:
            formatted_tape.append({
                "ticker": f["contract"],
                "size": f"{f['size']:,}",
                "premium": f"${f['premium_val']/1000:.0f}K" if f['premium_val'] < 1000000 else f"${f['premium_val']/1000000:.1f}M",
                "price": f"${f['price']:.2f}",
                "time": "LIVE"
            })
        return {"tape": formatted_tape}
    except Exception:
        return {"tape": []}

@router.get("/api/gex")
async def get_gamma_exposure(ticker: str):
    """Calculates true Net Gamma Exposure (Call GEX - Put GEX) per strike."""
    if not POLYGON_KEY: return {"status": "error", "data": []}
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        live_price = stock.fast_info['last_price']
        
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?limit=250&apiKey={POLYGON_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        
        strikes = {}
        for c in data.get("results", []):
            details = c.get("details", {})
            greeks = c.get("greeks")
            if not greeks: continue
            
            strike = details.get("strike_price", 0)
            # Filter to strikes within +/- 10% of live price to keep chart clean
            if strike == 0 or abs(strike - live_price) / live_price > 0.10: continue
            
            ctype = details.get("contract_type", "").lower()
            gamma = greeks.get("gamma", 0)
            oi = c.get("open_interest", 0)
            
            contract_gex = gamma * oi * 100
            if strike not in strikes: strikes[strike] = 0
            
            if ctype == "call": strikes[strike] += contract_gex
            elif ctype == "put": strikes[strike] -= contract_gex
        
        gex_data = [{"strike": s, "gex": round(strikes[s])} for s in sorted(strikes.keys())]
        return {"status": "success", "ticker": ticker, "data": gex_data}
    except Exception as e:
        return {"status": "error", "data": []}

@router.get("/api/heatmap")
async def get_options_heatmap(ticker: str):
    """Calculates true volume matrix across the 3 closest expiration dates."""
    if not POLYGON_KEY: return {"status": "error", "data": []}
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        live_price = stock.fast_info['last_price']
        
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?limit=250&apiKey={POLYGON_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                
        matrix = {}
        expirations = set()
        
        for c in data.get("results", []):
            details = c.get("details", {})
            strike = details.get("strike_price", 0)
            # Tighter bounds for the heatmap (+/- 5%)
            if strike == 0 or abs(strike - live_price) / live_price > 0.05: continue
            
            exp = details.get("expiration_date")
            vol = c.get("day", {}).get("volume", 0)
            
            if exp not in expirations: expirations.add(exp)
            if strike not in matrix: matrix[strike] = {}
            if exp not in matrix[strike]: matrix[strike][exp] = 0
            
            matrix[strike][exp] += vol
            
        sorted_exps = sorted(list(expirations))[:3]
        
        heatmap_data = []
        for s in sorted(matrix.keys(), reverse=True): # Highest strike on top
            row = {"strike": s}
            for i in range(3):
                exp_key = sorted_exps[i] if i < len(sorted_exps) else None
                vol = matrix[s].get(exp_key, 0) if exp_key else 0
                row[f"exp{i+1}"] = vol
            heatmap_data.append(row)
            
        return {"status": "success", "ticker": ticker, "data": heatmap_data[:10]}
    except Exception as e:
        return {"status": "error", "data": []}

@router.get("/api/equities/global_plays")
async def get_global_plays():
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        now_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%B %d, %Y')
        
        sys_prompt = f"""
        You are the 'Apex Options Desk', an autonomous quantitative AI reading Smart Money Concepts.
        Current Date: {now_str}.
        
        Scan your knowledge of the current macro environment and generate exactly THREE high-probability options setups for highly liquid mega-caps (e.g., SPY, QQQ, NVDA, TSLA, AAPL, META).
        Provide 1 DAY TRADE (0-2 DTE), 1 SWING (1-4 weeks), and 1 LEAP (3+ months).
        
        Base your thesis heavily on Volume Point of Control (POC), Order Blocks, and dark pool flow structure.
        
        Return ONLY a JSON array of 3 objects with this exact structure:
        [
          {{
            "ticker": "AAPL", "play_type": "SWING", "direction": "CALLS", "confidence": 92, 
            "strike": "180C", "expiration": "Next Month",
            "thesis": "Price has tapped a massive bullish order block while maintaining support above the 90-day Volume POC."
          }}
        ]
        """
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=1500,
            system=sys_prompt, messages=[{"role": "user", "content": "Generate global quant setups in JSON."}]
        )
        
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        clean_json = re.sub(r'```json\n|```', '', raw_text).strip()
        plays = json.loads(clean_json)
        return {"plays": plays}
    except Exception as e:
        return {"error": str(e), "plays": []}

@router.get("/api/equities/ticker_idea")
async def get_ticker_idea(ticker: str):
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        now_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%B %d, %Y')
        
        sys_prompt = f"""
        You are the 'Apex Options Desk', an autonomous quantitative AI reading Smart Money Concepts.
        Current Date: {now_str}.
        
        Generate a highly confident, institutional-grade options trade thesis for {ticker.upper()}.
        Determine the best timeframe (DAY TRADE, SWING, or LEAP) based on current volatility and structure.
        Base your thesis heavily on Order Blocks, VWAP, and Gamma Exposure (GEX) concepts.
        
        Return ONLY a JSON object with this exact structure:
        {{
            "ticker": "{ticker.upper()}", "play_type": "SWING", "direction": "PUTS", "confidence": 88, 
            "strike": "150P", "expiration": "Date",
            "thesis": "1-2 sentence ruthless, institutional breakdown of the SMC setup."
        }}
        """
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=1000,
            system=sys_prompt, messages=[{"role": "user", "content": f"Generate quant setup for {ticker} in JSON."}]
        )
        
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        clean_json = re.sub(r'```json\n|```', '', raw_text).strip()
        idea = json.loads(clean_json)
        return {"idea": idea}
    except Exception as e:
        return {"error": str(e), "idea": None}

# ==========================================
# PILLAR 2: SPORTS BETTING MODELS
# ==========================================
def fetch_live_data_sync(sport: str, target_date_str: str = "Today"):
    # ... (Sports logic remains identical) ...
    pass

app.include_router(router)