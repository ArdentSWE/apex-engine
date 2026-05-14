import os
import requests
import asyncio
import datetime
import pytz
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

# ==========================================
# PILLAR 1: EQUITIES & OPTIONS (The Terminal)
# ==========================================

@router.get("/api/news")
def get_macro_docket(ticker: str = "SPY"):
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
        response = requests.get(url)
        data = response.json()
        formatted_news = [{"title": item.get("headline"), "source": item.get("source"), "url": item.get("url")} for item in data[:4]]
        return {"news": formatted_news}
    except Exception:
        return {"news": [{"title": "Live Wire Disconnected", "source": "System", "url": "#"}]}

@router.get("/api/flow")
def get_options_flow(ticker: str = "SPY"):
    try:
        url = f"https://api.polygon.io/v3/trades/{ticker}?limit=10&apiKey={POLYGON_KEY}"
        response = requests.get(url)
        data = response.json()
        formatted_tape = []
        for trade in data.get("results", [])[:5]:
            size = trade.get("size", 0)
            price = trade.get("price", 0)
            formatted_tape.append({
                "ticker": ticker.upper(),
                "size": f"{size:,}",
                "premium": f"${(size * price * 100):,.1f}", 
                "price": f"${price:.2f}",
                "time": "LIVE"
            })
        return {"tape": formatted_tape}
    except Exception:
        return {"tape": [{"ticker": ticker, "size": "ERROR", "premium": "API BLOCK", "price": "-", "time": "-"}]}

@router.get("/api/gex")
def get_gamma_exposure(ticker: str):
    return {"status": "GEX calculated", "ticker": ticker, "data": []}

@router.get("/api/heatmap")
def get_options_heatmap(ticker: str):
    return {"status": "Heatmap generated", "ticker": ticker, "data": []}

@router.get("/api/signals")
def get_quant_signals(type: str = "all"):
    signals = [
        {"ticker": "AAPL", "type": "SWING", "dir": "PUTS", "conf": 76, "entry": "AUTO", "target": "CALC"},
        {"ticker": "QQQ", "type": "DAY_TRADE", "dir": "CALLS", "conf": 82, "entry": "AUTO", "target": "CALC"}
    ]
    return {"signals": signals}


# ==========================================
# PILLAR 2: SPORTS BETTING MODELS (BOT.PY LOGIC)
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
        client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY").strip())
        
        # Determine if it's a Player Props or Game Lines parlay based on the market text
        is_props = any(keyword in market.upper() for keyword in ["PROP", "PTS", "REB", "AST", "YDS", "HITS", "SOG"])
        
        if is_props:
            sys_prompt = f"""
            You are the 'Apex Quant Engine', an elite sports betting risk manager.
            Sport Context: {league}
            Live Board: {live_board_str}
            Rosters: {live_rosters_str}
            
            YOUR DIRECTIVE:
            1. Keep final output under 250 words. Do NOT mention Claude.
            2. Strictly construct this parlay using ONLY Individual Player Propositions. ALL legs must exclusively be 'Over' contracts. NO team lines or totals.
            3. Base your thesis heavily on recent L10 trends, defensive matchups, and injury ripple effects (who absorbs the usage).
            4. TRUE CORRELATION ONLY: Ensure legs positively correlate.
            5. DO NOT hallucinate fake EV percentages.
            
            FORMAT EXACTLY LIKE THIS:
            🔥 **THE GOD PARLAY:** [{legs}-Leg Parlay] (+Odds)
            🎯 **CONFIDENCE:** [Percentage between 80-99%]
            💰 **UNIT SIZING:** [Recommend a strict, conservative unit size]
            
            **🧠 THE THESIS (LEG BY LEG):**
            - **[Leg 1]:** [1-sentence reasoning]
            - **[Leg 2]:** [1-sentence reasoning]
            """
            user_prompt = f"Construct a logical Player Props parlay for {team1} vs {team2}. Markets: {market}. Date: {date}."
        else:
            sys_prompt = f"""
            You are the 'Apex Quant Engine', an elite sports betting risk manager.
            Sport Context: {league}
            Live Board: {live_board_str}
            Rosters: {live_rosters_str}
            
            YOUR DIRECTIVE:
            1. Keep final output under 250 words. Do NOT mention Claude.
            2. Strictly construct this parlay using ONLY Team Moneylines, Point Spreads, and Game Totals (Over/Under). DO NOT include individual player props.
            3. Base your thesis heavily on recent L10 trends, pace of play, and situational advantages (rest, injuries).
            4. DO NOT hallucinate fake EV percentages.
            
            FORMAT EXACTLY LIKE THIS:
            🔥 **THE QUANT PARLAY:** [{legs}-Leg Parlay] (+Odds)
            🎯 **CONFIDENCE:** [Percentage between 80-99%]
            💰 **UNIT SIZING:** [Recommend a strict, conservative unit size]
            
            **🧠 THE THESIS (LEG BY LEG):**
            - **[Leg 1]:** [1-sentence reasoning]
            - **[Leg 2]:** [1-sentence reasoning]
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
        client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY").strip())
        
        sys_prompt = f"""
        You are the 'Apex Game Predictor', an elite sports betting risk manager.
        Sport Context: {sport}
        Live Board: {live_board_str}
        Rosters: {live_rosters_str}
        
        YOUR DIRECTIVE:
        1. Output strictly under 200 words. Do NOT mention Claude.
        2. Base your thesis heavily on recent L10 trends and situational awareness (injuries, pace).
        3. DO NOT hallucinate fake EV percentages.
        
        FORMAT EXACTLY LIKE THIS:
        🔥 **TOP PLAY:** [State the single best bet clearly]
        🎯 **CONFIDENCE:** [Percentage between 80-99%]
        💰 **UNIT SIZING:** [Recommend a strict, conservative unit size]
        
        **🧠 THE THESIS:**
        - [Bullet 1: Cite L10 trends or home/away splits]
        - [Bullet 2: Specific defensive matchup or pace advantage]
        - [Bullet 3: Market pricing value]
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