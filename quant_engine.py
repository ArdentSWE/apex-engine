import time
import requests
import datetime
import pytz
from duckduckgo_search import DDGS
from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelog

# ==========================================
# ⚡ TOOL 1: THE AUTONOMOUS WEB SCRAPER
# ==========================================
def search_live_web(query: str, max_results: int = 3) -> str:
    """Scours the internet strictly for live Vegas odds, injury reports, and real-time news."""
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return f"WEB SEARCH FAILED: No recent data found for '{query}'."
        
        compiled_data = f"--- LIVE WEB DATA FOR '{query}' ---\n"
        for r in results:
            compiled_data += f"Title: {r.get('title')}\nSnippet: {r.get('body')}\n\n"
        return compiled_data
    except Exception as e:
        return f"WEB SEARCH ERROR: {str(e)}"

# ==========================================
# ⚡ TOOL 2: THE REALITY CHECK (SCHEDULE VERIFIER)
# ==========================================
def get_todays_slate(sport: str, date_str: str = "today") -> str:
    """Pulls the exact, verified schedule for a specific date to prevent hallucinations."""
    sport = sport.upper()
    espn_routes = {
        "NBA": ("basketball", "nba"),
        "NFL": ("football", "nfl"),
        "MLB": ("baseball", "mlb"),
        "NHL": ("hockey", "nhl"),
        "SOC": ("soccer", "eng.1"),          
        "CRICKET": ("cricket", "mens-international-cricket") 
    }
    
    route = espn_routes.get(sport)
    if not route:
        return f"Error: Sport '{sport}' not supported by schedule verifier."
        
    # Calculate the correct date for the ESPN API
    now = datetime.datetime.now(pytz.timezone('America/New_York'))
    if date_str.strip().lower() == "tomorrow":
        target_date = now + datetime.timedelta(days=1)
    elif date_str.strip().lower() == "yesterday":
        target_date = now - datetime.timedelta(days=1)
    else:
        target_date = now
        
    formatted_date = target_date.strftime('%Y%m%d')
    url = f"https://site.api.espn.com/apis/site/v2/sports/{route[0]}/{route[1]}/scoreboard?dates={formatted_date}"
    
    try:
        response = requests.get(url, timeout=8)
        if response.status_code != 200:
            return f"Schedule API Error. Could not verify games for {date_str}."
            
        data = response.json()
        events = data.get('events', [])
        
        if not events:
            return f"VERIFIED: No {sport} games scheduled for {date_str} ({formatted_date})."
            
        schedule = []
        for event in events:
            competitors = event.get('competitions', [{}])[0].get('competitors', [])
            if len(competitors) < 2: continue
            
            home_team = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0]).get('team', {}).get('displayName', 'HOME')
            away_team = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1]).get('team', {}).get('displayName', 'AWAY')
            status = event.get('status', {}).get('type', {}).get('description', 'Scheduled')
            
            schedule.append(f"[{status}] {away_team} @ {home_team}")
            
        return f"--- VERIFIED SLATE FOR {sport} ON {date_str.upper()} ---\n" + "\n".join(schedule)
        
    except Exception as e:
        return f"Schedule Fetch Error: {str(e)}"

# ==========================================
# ⚡ TOOL 3: FREE L20 BACKTESTING
# ==========================================
NBA_STATS_CACHE = {}

def get_free_l20_hit_rate(player_name: str, stat_type: str, target_line: float) -> str:
    """Zero-cost L20 Backtesting with Anti-Bot Spoofing & Retries."""
    global NBA_STATS_CACHE
    
    nba_players = players.get_players()
    target_player = next((p for p in nba_players if player_name.lower() in p['full_name'].lower()), None)
    
    if not target_player:
        return f"ANTI-HALLUCINATION PROTOCOL: Player '{player_name}' not found in official database. ABORT LEG."
        
    pid = target_player['id']
    cache_key = f"{pid}_{stat_type}_{target_line}"
    
    if cache_key in NBA_STATS_CACHE:
        return NBA_STATS_CACHE[cache_key]
        
    custom_headers = {
        'Host': 'stats.nba.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.nba.com/',
        'Origin': 'https://www.nba.com/',
        'Connection': 'keep-alive',
    }
        
    max_retries = 3
    for attempt in range(max_retries):
        try:
            log = playergamelog.PlayerGameLog(player_id=pid, season='2025-26', headers=custom_headers, timeout=10)
            df = log.get_data_frames()[0]
            
            if df.empty:
                return f"ANTI-HALLUCINATION PROTOCOL: No game logs found for '{player_name}'. ABORT LEG."
                
            stat_map = {"PTS": "PTS", "REB": "REB", "AST": "AST", "THREES": "FG3M", "STL": "STL", "BLK": "BLK", "TOV": "TOV"}
            col = stat_map.get(stat_type.upper())
            
            if not col or col not in df.columns:
                return f"ANTI-HALLUCINATION PROTOCOL: Stat '{stat_type}' invalid. ABORT LEG."
                
            l20_df = df.head(20)
            total_games = len(l20_df)
            
            if total_games == 0:
                return f"ANTI-HALLUCINATION PROTOCOL: Zero recent games played for '{player_name}'. ABORT LEG."

            hits = (l20_df[col] > float(target_line)).sum()
            hit_rate = (hits / total_games) * 100
            avg_stat = l20_df[col].mean()
            
            result = (f"L20 BACKTEST VERIFIED: {target_player['full_name']} hit OVER {target_line} {stat_type} in {hits}/{total_games} games.\n"
                      f"Hit Rate: {hit_rate:.1f}% | L20 Average: {avg_stat:.1f}")
            
            NBA_STATS_CACHE[cache_key] = result
            time.sleep(1.0)
            return result
            
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2) 
                continue
            return f"ANTI-HALLUCINATION PROTOCOL: Stats engine failed after {max_retries} attempts ({str(e)}). ABORT LEG."