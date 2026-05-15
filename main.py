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
        
        # 1. Volume Point of Control (POC)
        hist['Price_Bins'] = pd.cut(hist['Close'], bins=30)
        vol_profile = hist.groupby('Price_Bins', observed=False)['Volume'].sum()
        poc_price = vol_profile.idxmax().mid
        
        # 2. Order Blocks
        recent = hist.tail(30).copy()
        recent['Return'] = recent['Close'].pct_change()
        best_day_loc = recent.index.get_loc(recent['Return'].idxmax())
        worst_day_loc = recent.index.get_loc(recent['Return'].idxmin())
        
        bullish_ob = 0
        bearish_ob = float('inf')
        
        if best_day_loc > 0:
            ob_candle = recent.iloc[best_day_loc - 1]
            if ob_candle['Close'] < ob_candle['Open']: bullish_ob = ob_candle['Low']
        if worst_day_loc > 0:
            ob_candle = recent.iloc[worst_day_loc - 1]
            if ob_candle['Close'] > ob_candle['Open']: bearish_ob = ob_candle['High']
            
        # 3. Polygon Live Flow & Gamma
        call_prem, put_prem, net_gamma = 0, 0, 0
        if POLYGON_KEY:
            url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?limit=250&apiKey={POLYGON_KEY}"
            async with aiohttp.ClientSession() as session:
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
                flow_list.append({
                    "contract": f"{strike}{ctype[0]} ({exp[5:]})",
                    "size": vol,
                    "premium_val": premium,
                    "price": vwap
                })
        
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
            if strike == 0 or abs(strike - live_price) / live_price > 0.05: continue
            
            exp = details.get("expiration_date")
            vol = c.get("day", {}).get("volume", 0)
            
            if exp not in expirations: expirations.add(exp)
            if strike not in matrix: matrix[strike] = {}
            if exp not in matrix[strike]: matrix[strike][exp] = 0
            
            matrix[strike][exp] += vol
            
        sorted_exps = sorted(list(expirations))[:3]
        
        heatmap_data = []
        for s in sorted(matrix.keys(), reverse=True): 
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
    """Endpoint for the Global Dashboard to fetch Swings, Leaps, and Day Trades."""
    file_path = "data/website_global_plays.json"
    # Fallback to local path if running outside Docker
    if not os.path.exists(file_path):
        file_path = "website_global_plays.json"
        
    try:
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                data = json.load(f)
            return JSONResponse(content={"plays": data.get("plays", [])})
        else:
            # Return empty list if the file hasn't been created by the bot yet
            return JSONResponse(content={"plays": []})
    except Exception as e:
        print(f"Error reading global plays: {e}")
        return JSONResponse(content={"plays": []}, status_code=500)

@router.get("/api/flow/whales")
async def get_whale_tape():
    """Endpoint for the Whale Flow Marquee to fetch the latest massive options sweeps."""
    file_path = "data/website_whale_tape.json"
    # Fallback to local path if running outside Docker
    if not os.path.exists(file_path):
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
        You are the 'Apex Options Desk', an autonomous quantitative AI.
        Current Date: {now_str}.
        
        Here is the exact live structural data for {ticker.upper()}:
        {context_data}
        
        Construct an institutional options trade thesis based STRICTLY on this data. Do not hallucinate levels. Reference the POC, Order Blocks, and premium flow in your thesis.
        Determine the best timeframe (DAY TRADE, SWING, or LEAP) based on structure.
        
        Return ONLY a JSON object with this exact structure:
        {{
            "ticker": "{ticker.upper()}", "play_type": "SWING", "direction": "PUTS", "confidence": 88, 
            "strike": "150P", "expiration": "Date",
            "thesis": "1-2 sentence ruthless, institutional breakdown using the provided math."
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

class OracleRequest(BaseModel):
    prompt: str
    ticker: str = "SPY"

@router.post("/api/oracle/query")
async def oracle_query(request: OracleRequest):
    """Intercepts user queries, fetches live Polygon data, and routes to Claude."""
    user_prompt = request.prompt
    target_ticker = request.ticker.upper()

    if not POLYGON_KEY or not ANTHROPIC_KEY:
        raise HTTPException(status_code=500, detail="Missing API Keys in Backend.")

    # 1. Fetch the raw tape from Polygon first
    url = f"https://api.polygon.io/v3/snapshot/options/{target_ticker}?limit=250&apiKey={POLYGON_KEY}"
    
    raw_flow_context = ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    chain_data = await resp.json()
                    
                    # Aggregate the data so we don't blow up Claude's token limit
                    total_call_vol, total_put_vol = 0, 0
                    total_call_oi, total_put_oi = 0, 0
                    
                    for c in chain_data.get("results", []):
                        ctype = c.get("details", {}).get("contract_type", "").lower()
                        vol = c.get("day", {}).get("volume", 0)
                        oi = c.get("open_interest", 0)
                        
                        if ctype == "call": 
                            total_call_vol += vol
                            total_call_oi += oi
                        elif ctype == "put": 
                            total_put_vol += vol
                            total_put_oi += oi
                            
                    raw_flow_context = (
                        f"Live {target_ticker} Options Data:\n"
                        f"- Volume: {total_call_vol:,} Calls vs {total_put_vol:,} Puts.\n"
                        f"- Open Interest: {total_call_oi:,} Calls vs {total_put_oi:,} Puts."
                    )
                else:
                    raw_flow_context = f"Warning: Could not retrieve live data for {target_ticker}."
    except Exception as e:
        print(f"Polygon fetch error: {e}")
        raw_flow_context = "Warning: Live data fetch failed."

    # 2. Inject Polygon Data into Claude Opus 4.7
    client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
    
    sys_prompt = f"""
    You are the 'Apex Oracle', an elite Wall Street AI.
    The user is asking about {target_ticker}. 
    
    [LIVE POLYGON.IO DATA]: 
    {raw_flow_context}
    
    Using the live data, answer the user's query. Provide a ruthless, institutional breakdown of the options flow. 
    Do not give financial advice. Keep your answer under 150 words. Use Discord markdown formatting.
    """
    
    try:
        res = await client.messages.create(
            model="claude-opus-4-7", 
            max_tokens=300,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        analysis = next((b.text for b in res.content if getattr(b, 'type', '') == 'text'), "Analysis failed.")
        return JSONResponse(content={"analysis": analysis})
        
    except Exception as e:
        print(f"Anthropic error: {e}")
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
            You are the 'Apex Quant Engine', an elite sports betting risk manager for the Ace's House Syndicate.
            Sport Context: {league}
            Live Board: {live_board_str}
            Rosters: {live_rosters_str}
            
            YOUR DIRECTIVE:
            1. Keep final output under 300 words. Do NOT mention Claude or Anthropic.
            2. Strictly construct this parlay using ONLY Individual Player Propositions. ALL legs must exclusively be 'Over' contracts.
            3. Base your thesis heavily on recent L10 trends, defensive matchups, and injury ripple effects.
            4. BRANDING: You MUST format the output exactly as shown below. DO NOT skip the disclosure.
            
            FORMAT EXACTLY LIKE THIS:
            ♠️ **ACE'S HOUSE QUANT DESK | GOD PARLAY**
            ━━━━━━━━━━━━━━━━━━━━━━
            🔥 **THE PLAY:** [{legs}-Leg Parlay] (+Odds)
            🎯 **ALGO CONFIDENCE:** [Percentage between 80-99%]
            💰 **UNIT SIZING:** [Recommend unit size]
            
            **🧠 THE THESIS (LEG BY LEG):**
            - **[Leg 1]:** [1-sentence reasoning]
            - **[Leg 2]:** [1-sentence reasoning]
            
            ━━━━━━━━━━━━━━━━━━━━━━
            ⚠️ **INSTITUTIONAL RISK DISCLOSURE**
            *This data is provided by the Ace's House algorithmic network for educational and entertainment purposes only. It is NOT financial or betting advice. Wagering carries extreme variance and financial risk. You are solely responsible for your own bankroll and capital. Tail strictly at your own risk.*
            """
            user_prompt = f"Construct a logical Player Props parlay for {team1} vs {team2}. Markets: {market}. Date: {date}."
        else:
            sys_prompt = f"""
            You are the 'Apex Quant Engine', an elite sports betting risk manager for the Ace's House Syndicate.
            Sport Context: {league}
            Live Board: {live_board_str}
            Rosters: {live_rosters_str}
            
            YOUR DIRECTIVE:
            1. Keep final output under 300 words. Do NOT mention Claude or Anthropic.
            2. Strictly construct this parlay using ONLY Team Moneylines, Point Spreads, and Game Totals (Over/Under). DO NOT include individual player props.
            3. Base your thesis heavily on recent L10 trends, pace of play, and situational advantages.
            
            FORMAT EXACTLY LIKE THIS:
            ♠️ **ACE'S HOUSE QUANT DESK | GAME LINES PARLAY**
            ━━━━━━━━━━━━━━━━━━━━━━
            🔥 **THE PLAY:** [{legs}-Leg Parlay] (+Odds)
            🎯 **ALGO CONFIDENCE:** [Percentage between 80-99%]
            💰 **UNIT SIZING:** [Recommend unit size]
            
            **🧠 THE THESIS (LEG BY LEG):**
            - **[Leg 1]:** [1-sentence reasoning]
            - **[Leg 2]:** [1-sentence reasoning]
            
            ━━━━━━━━━━━━━━━━━━━━━━
            ⚠️ **INSTITUTIONAL RISK DISCLOSURE**
            *This data is provided by the Ace's House algorithmic network for educational and entertainment purposes only. It is NOT financial or betting advice. Wagering carries extreme variance and financial risk. You are solely responsible for your own bankroll and capital. Tail strictly at your own risk.*
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
        You are the 'Apex Game Predictor', an elite sports betting risk manager for the Ace's House Syndicate.
        Sport Context: {sport}
        Live Board: {live_board_str}
        Rosters: {live_rosters_str}
        
        YOUR DIRECTIVE:
        1. Output strictly under 300 words. Do NOT mention Claude.
        2. Base your thesis heavily on recent L10 trends and situational awareness.
        
        FORMAT EXACTLY LIKE THIS:
        ♠️ **ACE'S HOUSE QUANT DESK | GAME PREDICTOR**
        ━━━━━━━━━━━━━━━━━━━━━━
        🔥 **TOP PLAY:** [State the single best bet clearly]
        🎯 **ALGO CONFIDENCE:** [Percentage between 80-99%]
        💰 **UNIT SIZING:** [Recommend unit size]
        
        **🧠 THE THESIS:**
        - [Bullet 1: Cite L10 trends or home/away splits]
        - [Bullet 2: Specific defensive matchup or pace advantage]
        - [Bullet 3: Market pricing value]
        
        ━━━━━━━━━━━━━━━━━━━━━━
        ⚠️ **INSTITUTIONAL RISK DISCLOSURE**
        *This data is provided by the Ace's House algorithmic network for educational and entertainment purposes only. It is NOT financial or betting advice. Wagering carries extreme variance and financial risk. You are solely responsible for your own bankroll and capital. Tail strictly at your own risk.*
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

app.include_router(router)