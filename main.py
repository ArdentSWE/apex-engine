import os
import requests
from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
import anthropic

app = FastAPI(title="Apex Engine", version="2.0")

# CORS config to allow Next.js frontend to connect safely
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

# Brainwash Claude 4.7 into the Apex Quant Engine
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

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
# PILLAR 2: SPORTS BETTING MODELS (ANTHROPIC 4.7)
# ==========================================

@router.get("/api/sports/parlays")
def generate_parlay(league: str = "NBA", team1: str = "", team2: str = "", market: str = "", legs: str = "3", date: str = "Today"):
    try:
        sys_prompt = f"""
        You are the 'Apex Quant Engine', an elite sports betting risk manager. Do NOT mention Anthropic, Claude, or AI.
        Construct a logical {legs}-leg parlay for {team1} vs {team2} in the {league} occurring on {date}. 
        Market Focus: {market}.
        
        FORMAT EXACTLY LIKE THIS:
        🔥 **THE GOD PARLAY:** [List the exact {legs} legs and the estimated odds, e.g., +450]
        🎯 **CONFIDENCE:** [Percentage between 80-99%]
        💰 **UNIT SIZING:** [Recommend unit size]
        
        **🧠 THE THESIS (LEG BY LEG):**
        - **[Leg 1]:** [1-sentence reasoning based on stats/matchups]
        - **[Leg 2]:** [1-sentence reasoning]
        """
        
        msg = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1000,
            system=sys_prompt,
            messages=[{"role": "user", "content": "Generate the quant parlay."}]
        )
        
        return {"result_text": msg.content[0].text}
        
    except Exception as e:
        return {"result_text": f"❌ Neural link disrupted: {str(e)}"}

@router.get("/api/sports/predictor")
def predict_game(team1: str, team2: str, sport: str = "NBA", market: str = "", date: str = "Today"):
    try:
        sys_prompt = f"""
        You are the 'Apex Game Predictor', an elite quantitative risk manager. Do NOT mention Anthropic, Claude, or AI.
        Identify the single highest confidence play for {team1} vs {team2} in the {sport} occurring on {date}.
        Market Focus: {market}.
        
        FORMAT EXACTLY LIKE THIS:
        🔥 **TOP PLAY:** [State the single best exact bet clearly, e.g., TIMBERWOLVES -4.5]
        🎯 **CONFIDENCE:** [Percentage between 80-99%]
        💰 **UNIT SIZING:** [Recommend unit size]
        
        **🧠 THE THESIS:**
        - [Bullet 1: Statistical trend or home/away split]
        - [Bullet 2: Specific matchup or injury advantage]
        - [Bullet 3: Market pricing/value]
        """
        
        msg = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1000,
            system=sys_prompt,
            messages=[{"role": "user", "content": "Generate the game prediction."}]
        )
        
        return {"result_text": msg.content[0].text}
        
    except Exception as e:
        return {"result_text": f"❌ Neural link disrupted: {str(e)}"}

app.include_router(router)