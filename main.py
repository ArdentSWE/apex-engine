import os
import requests
import asyncio
import datetime
import pytz
import json
import re
import aiohttp
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
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
# SMC MATH ENGINE 
# ==========================================

async def compute_smc_data(ticker: str):
    """Calculates true Volume POC, Order Blocks, and Flow for a ticker."""
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        hist = stock.history(period="90d")
        if hist.empty or len(hist) < 30: return None
        
        close = hist['Close'].iloc[-1]
        
        hist['Price_Bins'] = pd.cut(hist['Close'], bins=30)
        vol_profile = hist.groupby('Price_Bins', observed=False)['Volume'].sum()
        poc_price = vol_profile.idxmax().mid
        
        recent = hist.tail(30).copy()
        recent['Return'] = recent['Close'].pct_change()
        best_day_loc = recent.index.get_loc(recent['Return'].idxmax())
        worst_day_loc = recent.index.get_loc(recent['Return'].idxmin())
        
        bullish_ob = recent.iloc[best_day_loc - 1]['Low'] if best_day_loc > 0 and recent.iloc[best_day_loc - 1]['Close'] < recent.iloc[best_day_loc - 1]['Open'] else 0
        bearish_ob = recent.iloc[worst_day_loc - 1]['High'] if worst_day_loc > 0 and recent.iloc[worst_day_loc - 1]['Close'] > recent.iloc[worst_day_loc - 1]['Open'] else float('inf')
            
        call_prem, put_prem, net_gamma = 0, 0, 0
        if POLYGON_KEY:
            min_strike, max_strike = close * 0.90, close * 1.10
            today_str = datetime.datetime.now().strftime('%Y-%m-%d')
            url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&limit=250&apiKey={POLYGON_KEY}"
            pages = 0
            
            async with aiohttp.ClientSession() as session:
                while url and pages < 10:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for c in data.get("results", []):
                                ctype = c.get('details', {}).get('contract_type', '').lower()
                                oi = c.get('open_interest', 0)
                                vol = c.get('day', {}).get('volume', 0)
                                vwap = c.get('day', {}).get('vwap', 0)
                                
                                prem = vol * vwap * 100
                                if ctype == 'call': call_prem += prem
                                elif ctype == 'put': put_prem += prem
                                
                                greeks = c.get("greeks")
                                if greeks and oi > 100:
                                    gex = greeks.get('gamma', 0) * oi * 100
                                    if ctype == 'call': net_gamma += gex
                                    else: net_gamma -= gex
                            
                            next_url = data.get("next_url")
                            url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                            pages += 1
                        else:
                            break
        
        return {
            "ticker": ticker,
            "close": round(close, 2),
            "poc": round(poc_price, 2),
            "bullish_ob": round(bullish_ob, 2),
            "bearish_ob": round(bearish_ob, 2),
            "call_prem": call_prem,
            "put_prem": put_prem,
            "net_gamma": net_gamma
        }
    except Exception as e:
        print(f"SMC Error for {ticker}: {e}")
        return None

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
    if not POLYGON_KEY: return {"tape": []}
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        live_price = stock.fast_info['last_price']
        min_strike, max_strike = live_price * 0.90, live_price * 1.10
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&limit=250&apiKey={POLYGON_KEY}"
        flow_list = []
        pages = 0
        
        async with aiohttp.ClientSession() as session:
            while url and pages < 10:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("results", []):
                            details = c.get("details", {})
                            vol = c.get("day", {}).get("volume", 0)
                            vwap = c.get("day", {}).get("vwap", 0)
                            if vol > 0 and vwap > 0:
                                premium = vol * vwap * 100
                                ctype = details.get("contract_type", "").upper()
                                strike = details.get("strike_price")
                                exp = details.get("expiration_date")
                                flow_list.append({
                                    "contract": f"{strike}{ctype[0]} ({exp[5:]})",
                                    "size": vol,
                                    "premium_val": premium,
                                    "price": vwap
                                })
                        next_url = data.get("next_url")
                        url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                        pages += 1
                    else:
                        break
        
        flow_list.sort(key=lambda x: x["premium_val"], reverse=True)
        formatted_tape = []
        for f in flow_list[:15]: 
            formatted_tape.append({
                "ticker": f["contract"],
                "size": f"{f['size']:,}",
                "premium": f"${f['premium_val']/1000:.0f}K" if f['premium_val'] < 1000000 else f"${f['premium_val']/1000000:.1f}M",
                "price": f"${f['price']:.2f}",
                "time": "LIVE"
            })
        return {"tape": formatted_tape}
    except Exception as e:
        print(e)
        return {"tape": []}

@router.get("/api/gex")
async def get_gamma_exposure(ticker: str):
    if not POLYGON_KEY: return {"status": "error", "data": []}
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        live_price = stock.fast_info['last_price']
        min_strike, max_strike = live_price * 0.90, live_price * 1.10
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&limit=250&apiKey={POLYGON_KEY}"
        strikes = {}
        pages = 0
        
        async with aiohttp.ClientSession() as session:
            while url and pages < 10:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("results", []):
                            details = c.get("details", {})
                            greeks = c.get("greeks")
                            if not greeks: continue
                            
                            strike = details.get("strike_price", 0)
                            ctype = details.get("contract_type", "").lower()
                            gamma = greeks.get("gamma", 0)
                            oi = c.get("open_interest", 0)
                            
                            contract_gex = gamma * oi * 100
                            if strike not in strikes: strikes[strike] = 0
                            if ctype == "call": strikes[strike] += contract_gex
                            elif ctype == "put": strikes[strike] -= contract_gex
                            
                        next_url = data.get("next_url")
                        url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                        pages += 1
                    else:
                        break
        
        gex_data = [{"strike": s, "gex": round(strikes[s])} for s in sorted(strikes.keys())]
        return {"status": "success", "ticker": ticker, "data": gex_data}
    except Exception as e:
        return {"status": "error", "data": []}

@router.get("/api/heatmap")
async def get_options_heatmap(ticker: str):
    if not POLYGON_KEY: return {"status": "error", "data": []}
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker)
        live_price = stock.fast_info['last_price']
        min_strike, max_strike = live_price * 0.90, live_price * 1.10
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&limit=250&apiKey={POLYGON_KEY}"
        matrix = {}
        expirations = set()
        pages = 0
        
        async with aiohttp.ClientSession() as session:
            while url and pages < 8:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("results", []):
                            details = c.get("details", {})
                            strike = details.get("strike_price", 0)
                            exp = details.get("expiration_date")
                            vol = c.get("day", {}).get("volume", 0)
                            
                            if exp not in expirations: expirations.add(exp)
                            if strike not in matrix: matrix[strike] = {}
                            if exp not in matrix[strike]: matrix[strike][exp] = 0
                            
                            matrix[strike][exp] += vol
                            
                        next_url = data.get("next_url")
                        url = f"{next_url}&apiKey={POLYGON_KEY}" if next_url else None
                        pages += 1
                    else:
                        break
            
        sorted_exps = sorted(list(expirations))[:3]
        heatmap_data = []
        for s in sorted(matrix.keys(), reverse=True): 
            row = {"strike": s}
            for i in range(3):
                exp_key = sorted_exps[i] if i < len(sorted_exps) else None
                row[f"exp{i+1}"] = matrix[s].get(exp_key, 0) if exp_key else 0
            heatmap_data.append(row)
            
        return {"status": "success", "ticker": ticker, "data": heatmap_data[:10]}
    except Exception as e:
        return {"status": "error", "data": []}

@router.get("/api/equities/global_plays")
async def get_global_plays():
    combined_plays = []
    
    # 1. Pull Swings & LEAPs
    if os.path.exists("latest_scans.json"):
        try:
            with open("latest_scans.json", "r") as f:
                data = json.load(f)
                combined_plays.extend(data.get("plays", []))
        except: pass

    # 2. Pull 0DTE & Weekly Scalps
    if os.path.exists("latest_0dte_scans.json"):
        try:
            with open("latest_0dte_scans.json", "r") as f:
                data = json.load(f)
                combined_plays.extend(data.get("plays", []))
        except: pass

    # 3. Pull Manual Discord Bot Overrides
    if os.path.exists("website_global_plays.json"):
        try:
            with open("website_global_plays.json", "r") as f:
                data = json.load(f)
                combined_plays.extend(data.get("plays", []))
        except: pass

    return JSONResponse(content={"plays": combined_plays})

@router.get("/api/flow/whales")
async def get_whale_tape():
    file_path = "website_whale_tape.json"
    try:
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                data = json.load(f)
            return JSONResponse(content={"tape": data.get("whales", [])})
        else:
            return JSONResponse(content={"tape": []})
    except Exception as e:
        print(f"Error reading whale tape: {e}")
        return JSONResponse(content={"tape": []}, status_code=500)

@router.get("/api/chart")
async def get_chart_data(ticker: str = "SPY"):
    try:
        stock = await asyncio.to_thread(yf.Ticker, ticker.upper())
        df = await asyncio.to_thread(stock.history, period="5d", interval="15m")
        
        if df.empty:
            return JSONResponse(content={"chart": []})
            
        chart_data = []
        for index, row in df.iterrows():
            chart_data.append({
                "time": int(index.timestamp()),
                "open": round(float(row['Open']), 2),
                "high": round(float(row['High']), 2),
                "low": round(float(row['Low']), 2),
                "close": round(float(row['Close']), 2)
            })
            
        return JSONResponse(content={"chart": chart_data})
    except Exception as e:
        print(f"Chart Error on {ticker}: {e}")
        return JSONResponse(content={"chart": []}, status_code=500)

@router.get("/api/equities/ticker_idea")
async def get_ticker_idea(ticker: str):
    try:
        smc_data = await compute_smc_data(ticker.upper())
        if not smc_data:
            return {"error": "Insufficient market data.", "idea": None}
            
        context_data = f"[{smc_data['ticker']}] Live: ${smc_data['close']} | 90d POC: ${smc_data['poc']} | Bull OB: ${smc_data['bullish_ob']} | Bear OB: ${smc_data['bearish_ob']} | Call Prem: ${smc_data['call_prem']:,.0f} | Put Prem: ${smc_data['put_prem']:,.0f} | Net GEX: {smc_data['net_gamma']:,.0f}"

        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        now_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%B %d, %Y')
        
        sys_prompt = f"""
        You are the 'Apex Options Desk', an autonomous quantitative AI. Date: {now_str}.
        Here is the live data for {ticker.upper()}:
        {context_data}
        Return ONLY a JSON object:
        {{ "ticker": "{ticker.upper()}", "play_type": "SWING", "direction": "PUTS", "confidence": 88, "strike": "150P", "expiration": "Date", "thesis": "1 sentence breakdown." }}
        """
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=1000,
            system=sys_prompt, messages=[{"role": "user", "content": f"Generate quant setup for {ticker} in JSON."}]
        )
        
        raw_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        idea = json.loads(match.group()) if match else None
        return {"idea": idea}
    except Exception as e:
        return {"error": str(e), "idea": None}

class OracleRequest(BaseModel):
    prompt: str
    ticker: str = "SPY"

@router.post("/api/oracle/query")
async def oracle_query(request: OracleRequest):
    user_prompt = request.prompt
    target_ticker = request.ticker.upper()

    if not POLYGON_KEY or not ANTHROPIC_KEY:
        raise HTTPException(status_code=500, detail="Missing API Keys in Backend.")

    raw_flow_context = ""
    try:
        stock = await asyncio.to_thread(yf.Ticker, target_ticker)
        live_price = stock.fast_info['last_price']
        min_strike, max_strike = live_price * 0.90, live_price * 1.10
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        url = f"https://api.polygon.io/v3/snapshot/options/{target_ticker}?strike_price.gte={min_strike}&strike_price.lte={max_strike}&expiration_date.gte={today_str}&limit=250&apiKey={POLYGON_KEY}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    chain_data = await resp.json()
                    total_call_vol, total_put_vol = 0, 0
                    
                    for c in chain_data.get("results", []):
                        ctype = c.get("details", {}).get("contract_type", "").lower()
                        vol = c.get("day", {}).get("volume", 0)
                        if ctype == "call": total_call_vol += vol
                        elif ctype == "put": total_put_vol += vol
                            
                    raw_flow_context = f"Live {target_ticker} ATM Flow: {total_call_vol:,} Calls vs {total_put_vol:,} Puts."
                else:
                    raw_flow_context = f"Warning: Could not retrieve live data for {target_ticker}."
    except Exception as e:
        raw_flow_context = "Warning: Live data fetch failed."

    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    sys_prompt = f"""
    You are the 'Apex Oracle', an elite Wall Street AI.
    The user is asking about {target_ticker}. 
    [LIVE FLOW]: {raw_flow_context}
    Using the live data, answer the user's query. Provide a ruthless, institutional breakdown. 
    Keep your answer under 150 words.
    """
    
    try:
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=300,
            system=sys_prompt, messages=[{"role": "user", "content": user_prompt}]
        )
        analysis = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "Analysis failed.")
        return JSONResponse(content={"analysis": analysis})
    except Exception as e:
        raise HTTPException(status_code=500, detail="AI Analysis failed.")

# ==========================================
# PILLAR 2: SPORTS BETTING MODELS
# ==========================================
def fetch_live_data_sync(sport: str, target_date_str: str = "Today"):
    sport = sport.upper()
    espn_routes = {
        "NBA": ("basketball", "nba"), "NFL": ("football", "nfl"), "MLB": ("baseball", "mlb"),
        "NHL": ("hockey", "nhl"), "EPL": ("soccer", "eng.1"), "LALIGA": ("soccer", "esp.1"),       
        "SERIEA": ("soccer", "ita.1"), "BUNDES": ("soccer", "ger.1"), "UCL": ("soccer", "uefa.champions"), 
        "NCAAB": ("basketball", "mens-college-basketball"), "CRICKET": ("cricket", "mens-international-cricket") 
    }
    
    route = espn_routes.get(sport)
    if not route: return f"Error: Sport '{sport}' not supported by live telemetry.", "Unavailable"
        
    sport_group, league = route
    now_pt = datetime.datetime.now(pytz.timezone('America/Los_Angeles'))
    
    date_input = target_date_str.strip().lower()
    if date_input == "tomorrow": target_date = now_pt + datetime.timedelta(days=1)
    elif date_input == "yesterday": target_date = now_pt - datetime.timedelta(days=1)
    else: target_date = now_pt
        
    formatted_date = target_date.strftime('%Y%m%d')
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_group}/{league}/scoreboard?dates={formatted_date}"
    
    try:
        response = requests.get(url, timeout=8)
        if response.status_code != 200: return "ESPN API Error", "Unavailable"
            
        data = response.json()
        events = data.get('events', [])
        
        if not events: return f"No {sport} games scheduled for {target_date.strftime('%A, %b %d')}.", "No active rosters."
            
        board_strings, roster_strings = [], []
        for event in events:
            status = event.get('status', {}).get('type', {}).get('description', 'Unknown')
            competitors = event.get('competitions', [{}])[0].get('competitors', [])
            if len(competitors) < 2: continue
            
            home_team = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
            away_team = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])
            
            home_name, away_name = home_team.get('team', {}).get('abbreviation', 'HOME'), away_team.get('team', {}).get('abbreviation', 'AWAY')
            board_strings.append(f"[{status}] {away_name} @ {home_name}")
            
            for team in [home_team, away_team]:
                t_name = team.get('team', {}).get('abbreviation', 'UNK')
                injuries = team.get('injuries', [])
                if injuries:
                    injured_players = [f"{inj.get('athlete', {}).get('displayName', 'Unknown')} ({inj.get('status', 'Out')})" for inj in injuries]
                    if injured_players: roster_strings.append(f"🚨 {t_name} INJURIES/OUT: {', '.join(injured_players)}")
                        
        live_board = " | ".join(board_strings)
        live_rosters = "\n".join(list(set(roster_strings))) if roster_strings else "Standard rosters active."
        return live_board, live_rosters
    except Exception as e:
        return "Live board temporarily unavailable.", "Rosters unavailable."

@router.get("/api/sports/parlays")
async def generate_parlay(league: str = "NBA", team1: str = "", team2: str = "", market: str = "", legs: str = "3", date: str = "Today"):
    try:
        live_board_str, live_rosters_str = await asyncio.to_thread(fetch_live_data_sync, league, date)
        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        
        is_props = any(keyword in market.upper() for keyword in ["PROP", "PTS", "REB", "AST", "YDS", "HITS", "SOG"])
        
        if is_props:
            sys_prompt = f"""
            You are the 'Apex Quant Engine', an elite sports betting risk manager.
            Sport Context: {league}
            Live Board: {live_board_str}
            Rosters: {live_rosters_str}
            
            YOUR DIRECTIVE:
            1. Keep final output under 300 words. Do NOT mention Claude.
            2. Strictly construct this parlay using ONLY Individual Player Propositions. ALL legs must exclusively be 'Over' contracts.
            3. Base your thesis heavily on recent L10 trends, defensive matchups, and injury ripple effects.
            
            🚨 CRITICAL ANTI-HALLUCINATION PROTOCOL 🚨
            1. You are strictly forbidden from inventing geographical advantages (e.g., altitude), weather conditions, or travel fatigue narratives unless explicitly proven by the data. 
            2. Do NOT invent or assume player injuries. Only reference injuries if they are explicitly listed in the provided 'Rosters' data.
            3. Keep the thesis purely mathematical, matchup-based, and driven by the provided L10 trends.
            4. POSITIVE CORRELATION MANDATE: All legs in this parlay MUST be positively correlated mathematically or narratively. If predicting a fast-paced blowout, correlate with 'Unders' on the losing team's starters (due to 4th quarter resting) or 'Overs' on the winning team's pace-pushers. Explicitly state the correlation multiplier in your thesis.
            5. METRIC FORCING: You must justify your logic using advanced metrics specific to the sport (e.g., Usage Rate, True Shooting %, Pace Factor, DVOA, or Expected Goals). Do not use generic terms like "playing well."
            
            FORMAT EXACTLY LIKE THIS:
            ♠️ **ACE'S HOUSE QUANT DESK | GOD PARLAY**
            ━━━━━━━━━━━━━━━━━━━━━━
            🔥 **THE PLAY:** [{legs}-Leg Parlay] (+Odds)
            🎯 **ALGO CONFIDENCE:** [Percentage between 80-99%]
            📈 **IMPLIED EDGE:** [Calculate the percentage difference between the AI's confidence and standard market implied probability]
            🔗 **CORRELATION FACTOR:** [High / Medium / Low]
            💰 **UNIT SIZING:** [Recommend unit size]
            
            **🧠 THE THESIS (LEG BY LEG):**
            - **[Leg 1]:** [1-sentence reasoning including EV and Correlation]
            - **[Leg 2]:** [1-sentence reasoning including EV and Correlation]
            """
            user_prompt = f"Construct a logical Player Props parlay for {team1} vs {team2}. Markets: {market}. Date: {date}."
        else:
            sys_prompt = f"""
            You are the 'Apex Quant Engine', an elite sports betting risk manager.
            Sport Context: {league}
            Live Board: {live_board_str}
            Rosters: {live_rosters_str}
            
            YOUR DIRECTIVE:
            1. Keep final output under 300 words. Do NOT mention Claude.
            2. Strictly construct this parlay using ONLY Team Moneylines, Point Spreads, and Game Totals.
            3. Base your thesis heavily on recent L10 trends, pace of play, and situational advantages.
            
            🚨 CRITICAL ANTI-HALLUCINATION PROTOCOL 🚨
            1. You are strictly forbidden from inventing geographical advantages (e.g., altitude), weather conditions, or travel fatigue narratives unless explicitly proven by the data. 
            2. Do NOT invent or assume player injuries. Only reference injuries if they are explicitly listed in the provided 'Rosters' data.
            3. Keep the thesis purely mathematical, matchup-based, and driven by the provided L10 trends.
            4. POSITIVE CORRELATION MANDATE: All legs in this parlay MUST be positively correlated mathematically or narratively. Explicitly state the correlation multiplier in your thesis.
            5. METRIC FORCING: You must justify your logic using advanced metrics specific to the sport (e.g., Usage Rate, True Shooting %, Pace Factor, DVOA, or Expected Goals). Do not use generic terms like "playing well."
            
            FORMAT EXACTLY LIKE THIS:
            ♠️ **ACE'S HOUSE QUANT DESK | GAME LINES PARLAY**
            ━━━━━━━━━━━━━━━━━━━━━━
            🔥 **THE PLAY:** [{legs}-Leg Parlay] (+Odds)
            🎯 **ALGO CONFIDENCE:** [Percentage between 80-99%]
            📈 **IMPLIED EDGE:** [Calculate the percentage difference between the AI's confidence and standard market implied probability]
            🔗 **CORRELATION FACTOR:** [High / Medium / Low]
            💰 **UNIT SIZING:** [Recommend unit size]
            
            **🧠 THE THESIS (LEG BY LEG):**
            - **[Leg 1]:** [1-sentence reasoning including EV and Correlation]
            - **[Leg 2]:** [1-sentence reasoning including EV and Correlation]
            """
            user_prompt = f"Construct a logical Game Lines parlay for {team1} vs {team2}. Markets: {market}. Date: {date}."

        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=2000,
            system=sys_prompt, messages=[{"role": "user", "content": user_prompt}]
        )
        
        final_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        return {"result_text": final_text}
    except Exception as e:
        return {"result_text": f"❌ Data Processing Error: {str(e)}"}

@router.get("/api/sports/predictor")
async def predict_game(team1: str, team2: str, sport: str = "NBA", market: str = "", date: str = "Today"):
    try:
        live_board_str, live_rosters_str = await asyncio.to_thread(fetch_live_data_sync, sport, date)
        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        
        sys_prompt = f"""
        You are the 'Apex Game Predictor', an elite sports betting risk manager.
        Sport Context: {sport}
        Live Board: {live_board_str}
        Rosters: {live_rosters_str}
        
        YOUR DIRECTIVE:
        1. Output strictly under 300 words. Do NOT mention Claude.
        2. Base your thesis heavily on recent L10 trends and situational awareness.
        
        🚨 CRITICAL ANTI-HALLUCINATION PROTOCOL 🚨
        1. You are strictly forbidden from inventing geographical advantages (e.g., altitude), weather conditions, or travel fatigue narratives unless explicitly proven by the data. 
        2. Do NOT invent or assume player injuries. Only reference injuries if they are explicitly listed in the provided 'Rosters' data.
        3. Keep the thesis purely mathematical, matchup-based, and driven by the provided L10 trends.
        4. EXPECTED VALUE (EV) PROTOCOL: Do not simply predict who will win. You must identify a market inefficiency. Explain why the public or the sportsbooks are mispricing this specific matchup. 
        5. VARIANCE & TAIL OUTCOMES: Acknowledge the floor and ceiling of this prediction. Identify the single biggest "wrecking ball" variable that could invalidate this thesis (e.g., a specific player getting into early foul trouble, or a sudden shift in game pace).
        
        FORMAT EXACTLY LIKE THIS:
        ♠️ **ACE'S HOUSE QUANT DESK | GAME PREDICTOR**
        ━━━━━━━━━━━━━━━━━━━━━━
        🔥 **TOP PLAY:** [State the single best bet clearly]
        🎯 **ALGO CONFIDENCE:** [Percentage between 80-99%]
        📈 **IMPLIED EDGE:** [Calculate the percentage difference between the AI's confidence and standard market implied probability]
        💰 **UNIT SIZING:** [Recommend unit size]
        
        **🧠 THE THESIS:**
        - [EV & Market Inefficiency: Cite L10 trends or home/away splits driving this]
        - [Metric Forcing: Specific defensive matchup, DVOA, or pace advantage]
        
        ⚠️ **INVALIDATION TRIGGER:** [What specific live-game event means this bet is dead]
        """
        user_prompt = f"Identify the highest confidence play for {team1} vs {team2} on {date}. Market focus: {market}."
        
        res = await client.messages.create(
            model="claude-opus-4-7", max_tokens=2000,
            system=sys_prompt, messages=[{"role": "user", "content": user_prompt}]
        )
        
        final_text = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "").strip()
        return {"result_text": final_text}
    except Exception as e:
        return {"result_text": f"❌ Data Processing Error: {str(e)}"}

@router.get("/api/sports/ticker")
async def get_sports_ticker():
    """Lightning-fast route to feed the global frontend marquee."""
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        response = requests.get(url, timeout=3)
        data = response.json()
        
        games = []
        for event in data.get('events', []):
            status = event['status']['type']['shortDetail']
            comps = event['competitions'][0]['competitors']
            
            away_team = next(c for c in comps if c['homeAway'] == 'away')
            home_team = next(c for c in comps if c['homeAway'] == 'home')
            
            away_name = away_team['team']['abbreviation']
            home_name = home_team['team']['abbreviation']
            away_score = away_team.get('score', '')
            home_score = home_team.get('score', '')
            
            games.append(f"[{status}] {away_name} {away_score} - {home_name} {home_score}")
            
        active_parlay = "🚨 GOD PARLAY: Jokic O 26.5 PTS + Murray O 6.5 AST (Algo Edge: 94.2%)"
        
        return JSONResponse(content={"games": games, "parlay": active_parlay})
    except Exception as e:
        return JSONResponse(content={"games": ["SCANNING ESPN RELAYS..."], "parlay": "CALIBRATING QUANT MODELS..."})

app.include_router(router)