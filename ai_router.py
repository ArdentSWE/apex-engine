import os
import json
import asyncio
import datetime
import pytz
from anthropic import AsyncAnthropic

from quant_engine import search_live_web, get_free_l20_hit_rate, get_todays_slate

anthropic_client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())

def get_agent_tools():
    """Equips Opus 4.7 with custom python-based internet access, schedule verification, and free backtesting."""
    return [
        {
            "name": "get_todays_slate",
            "description": "Pulls the official, verified schedule. You MUST use this to verify a game is actually happening on the requested date before analyzing it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sport": {"type": "string", "description": "e.g., 'NBA', 'NFL', 'MLB', 'NHL', 'SOC', 'CRICKET'"},
                    "date_str": {"type": "string", "description": "Must be 'today', 'tomorrow', or 'yesterday'. Look at the user's prompt to determine which one to use."}
                },
                "required": ["sport", "date_str"]
            }
        },
        {
            "name": "search_live_web",
            "description": "Scours the internet strictly for live Vegas odds, injury reports, and real-time sportsbook lines.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "e.g., 'DraftKings NBA player prop odds today' or 'LeBron James injury status'"}},
                "required": ["query"]
            }
        },
        {
            "name": "get_free_l20_hit_rate",
            "description": "Calculates the exact L20 historical hit rate for a player prop. YOU MUST USE THIS TO VALIDATE LEGS.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string"},
                    "stat_type": {"type": "string", "description": "PTS, REB, AST, THREES, STL, BLK, TOV"},
                    "target_line": {"type": "number"}
                },
                "required": ["player_name", "stat_type", "target_line"]
            }
        }
    ]

async def route_intent(prompt: str) -> str:
    """Ultra-fast routing agent to categorize user intents for the !apex command."""
    sys_prompt = """
    Categorize the following user prompt into ONE exact string: "QUANT", "CONFIG", "RECRUITING", "BROADCAST", or "CHAT".
    Respond ONLY with the exact string.
    """
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=sys_prompt,
            messages=[{"role": "user", "content": prompt}]
        )
        action = next((b.text for b in response.content if getattr(b, 'type', '') == 'text'), "CHAT").strip().upper()
        return action if action in ["QUANT", "CONFIG", "RECRUITING", "BROADCAST", "CHAT"] else "CHAT"
    except Exception as e: 
        print(f"Routing Error: {e}")
        return "CHAT"

async def execute_omni_agent(mode: str, sport: str, live_board: str, user_prompt: str) -> str:
    now_time_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %I:%M %p EST')
    
    sys_prompt = f"""
    You are the Apex Quant Desk. Your only objective is mathematical certainty. CURRENT TIME: {now_time_str}.
    You are equipped with web scraping, schedule verification, and backtesting tools.

    THE ANTI-HALLUCINATION PROTOCOL:
    1. VERIFY: You must use `get_todays_slate` to confirm the requested game is happening on the requested date.
    2. PRICE: You must use `search_live_web` to find the exact live odds and lines for the props you are considering.
    3. BACKTEST: You MUST run `get_free_l20_hit_rate` on EVERY potential player leg.
    4. ABORT: If you cannot verify the game, cannot find live odds, or if the backtest returns under an 80% hit rate, you must discard the leg immediately. Do not guess. Do not estimate.

    Output your final, verified ticket with the exact historical hit rates provided by your tools in Discord markdown.
    """

    tools = get_agent_tools()
    messages = [{"role": "user", "content": user_prompt}]
    
    max_loops = 10 
    current_loop = 0

    while current_loop < max_loops:
        current_loop += 1
        tool_behavior = {"type": "any"} if current_loop == 1 else {"type": "auto"}
        
        try:
            response = await anthropic_client.messages.create(
                model="claude-opus-4-7", 
                max_tokens=4096,
                system=sys_prompt,
                tools=tools,
                tool_choice=tool_behavior, 
                messages=messages
            )
        except Exception as e:
            return f"❌ **System API Rejection:** {str(e)}"

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            
            for block in response.content:
                if block.type == "tool_use":
                    func_name = block.name
                    args = block.input
                    res_str = ""
                    
                    try:
                        if func_name == "get_free_l20_hit_rate":
                            res_str = await asyncio.to_thread(
                                get_free_l20_hit_rate, 
                                args.get('player_name'), args.get('stat_type'), args.get('target_line')
                            )
                        elif func_name == "search_live_web":
                            res_str = await asyncio.to_thread(
                                search_live_web, 
                                args.get('query')
                            )
                        elif func_name == "get_todays_slate":
                            res_str = await asyncio.to_thread(
                                get_todays_slate,
                                args.get('sport'),
                                args.get('date_str', 'today') # <--- Passing the date properly
                            )
                    except Exception as e:
                        res_str = f"Execution error: {str(e)}"
                        
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": res_str
                    })
            
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
        else:
            return next((block.text for block in response.content if block.type == "text"), "")

    return "❌ **Loop Limit Exceeded:** The Agent scoured the web and its databases but failed to converge on an 80% mathematically secure ticket in time."