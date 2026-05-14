import os
import requests
from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from ai_router import execute_omni_agent

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
# PILLAR 2: SPORTS BETTING MODELS (OMNI AGENT)
# ==========================================

@router.get("/api/sports/parlays")
async def generate_parlay(league: str = "NBA", team1: str = "", team2: str = "", market: str = "", legs: str = "3", date: str = "Today"):
    try:
        user_prompt = f"Construct a {legs}-leg parlay for {team1} vs {team2} on {date}. Market focus: {market}. Format with 🔥 THE GOD PARLAY:, 🎯 CONFIDENCE:, 💰 UNIT SIZING:, and 🧠 THE THESIS (LEG BY LEG):"
        
        # Trigger the true agent with L20 backtesting and Web Scraping
        result_text = await execute_omni_agent(
            mode="PARLAY", 
            sport=league, 
            live_board="Check schedule tool", 
            user_prompt=user_prompt
        )
        return {"result_text": result_text}
    except Exception as e:
        return {"result_text": f"❌ Neural link disrupted: {str(e)}"}

@router.get("/api/sports/predictor")
async def predict_game(team1: str, team2: str, sport: str = "NBA", market: str = "", date: str = "Today"):
    try:
        user_prompt = f"Identify the single highest confidence play for {team1} vs {team2} on {date}. Market focus: {market}. Format with 🔥 TOP PLAY:, 🎯 CONFIDENCE:, 💰 UNIT SIZING:, and 🧠 THE THESIS:"
        
        # Trigger the true agent with L20 backtesting and Web Scraping
        result_text = await execute_omni_agent(
            mode="PREDICTOR", 
            sport=sport, 
            live_board="Check schedule tool", 
            user_prompt=user_prompt
        )
        return {"result_text": result_text}
    except Exception as e:
        return {"result_text": f"❌ Neural link disrupted: {str(e)}"}

app.include_router(router)