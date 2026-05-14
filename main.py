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
    """Pulls live boards, home/away data, active rosters, and REAL-TIME INJURY SCRATCHES."""
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