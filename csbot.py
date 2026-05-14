import discord
from discord import app_commands
import aiohttp
import asyncio
import json
import os
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION — fill these in before deploying
# ============================================================

from dotenv import load_dotenv
load_dotenv("/home/cs2bot/.env")

DISCORD_TOKEN   = os.environ.get("DISCORD_TOKEN", "")
LEETIFY_API_KEY = os.environ.get("LEETIFY_API_KEY", "")
STEAM_API_KEY   = os.environ.get("STEAM_API_KEY", "")
CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY", "")
CHANNEL_ID      = 1502540342509965342  # legacy default, kept for reference

# ============================================================
# PER-SERVER CHANNEL CONFIG
# ============================================================

CHANNELS_FILE = "cs2bot_channels.json"

def default_channel_settings(channel_id):
    return {
        "channel_id": channel_id,
        "match_posts": True,
        "daily_summary": True,
        "weekly_summary": True,
    }

def load_channels():
    """Load channel configs. Returns dict of channel_id -> settings dict."""
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            cid = int(k)
            if isinstance(v, int):
                # Migrate old format (channel_id only)
                result[cid] = default_channel_settings(v)
            else:
                result[cid] = v
        return result
    # Seed with default channel
    return {CHANNEL_ID: default_channel_settings(CHANNEL_ID)}

def save_channels(channels):
    with open(CHANNELS_FILE, "w") as f:
        json.dump({str(k): v for k, v in channels.items()}, f, indent=2)

def get_channels_for(bot_instance, setting=None):
    """
    Return list of valid discord.TextChannel objects.
    If setting is provided (e.g. 'match_posts'), only return channels where that setting is True.
    """
    channels = load_channels()
    result = []
    for channel_id, cfg in channels.items():
        if setting and not cfg.get(setting, True):
            continue
        ch = bot_instance.get_channel(int(cfg.get("channel_id", channel_id)))
        if ch:
            result.append(ch)
    return result

# Keep get_all_channels as alias for backwards compat
def get_all_channels(bot_instance):
    return get_channels_for(bot_instance)



POLL_INTERVAL = 120  # seconds between polls

# ============================================================
# PLAYERS FILE — runtime-editable via slash commands
# ============================================================

PLAYERS_FILE = "cs2bot_players.json"

def load_players():
    if os.path.exists(PLAYERS_FILE):
        with open(PLAYERS_FILE, "r") as f:
            return json.load(f)
    return {"Jake": "76561198042824815"}

def save_players(players):
    with open(PLAYERS_FILE, "w") as f:
        json.dump(players, f, indent=2)

# ============================================================
# STATE FILE
# ============================================================

STATE_FILE = "cs2bot_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ============================================================
# LEETIFY API
# ============================================================

LEETIFY_BASE = "https://api-public.cs-prod.leetify.com"

# Steam IDs that returned 404 from Leetify, mapped to the timestamp of the 404.
# Entries expire after 1 hour so newly-registered accounts get retried automatically.
LEETIFY_404_CACHE: dict[str, float] = {}
LEETIFY_404_TTL = 3600  # seconds

def leetify_headers():
    if LEETIFY_API_KEY and LEETIFY_API_KEY != "YOUR_LEETIFY_API_KEY":
        return {"Authorization": f"Bearer {LEETIFY_API_KEY}"}
    return {}

async def fetch_recent_matches(session, steam_id):
    import time
    cached_at = LEETIFY_404_CACHE.get(steam_id)
    if cached_at and (time.time() - cached_at) < LEETIFY_404_TTL:
        return None
    url = f"{LEETIFY_BASE}/v3/profile/matches"
    params = {"steam64_id": steam_id}
    try:
        async with session.get(url, headers=leetify_headers(), params=params) as resp:
            if resp.status == 404:
                LEETIFY_404_CACHE[steam_id] = time.time()
                print(f"Leetify 404 for {steam_id} — will retry in {LEETIFY_404_TTL//60}min")
                return None
            if resp.status != 200:
                print(f"Leetify matches {steam_id}: HTTP {resp.status}")
                return None
            LEETIFY_404_CACHE.pop(steam_id, None)  # clear cache on success
            return await resp.json()
    except Exception as e:
        print(f"Leetify fetch error ({steam_id}): {e}")
        return None

async def fetch_profile(session, steam_id=None, leetify_id=None):
    url = f"{LEETIFY_BASE}/v3/profile"
    if steam_id:
        params = {"steam64_id": steam_id}
    elif leetify_id:
        params = {"id": leetify_id}
    else:
        return None
    try:
        async with session.get(url, headers=leetify_headers(), params=params) as resp:
            if resp.status != 200:
                print(f"Leetify profile HTTP {resp.status}")
                return None
            return await resp.json()
    except Exception as e:
        print(f"Leetify profile error: {e}")
        return None


# ============================================================
# STEAM API
# ============================================================

STEAM_API_BASE = "https://api.steampowered.com"

async def resolve_vanity_url(session, vanity_name):
    """Resolve a Steam vanity URL name to a Steam64 ID."""
    url = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1"
    params = {"key": STEAM_API_KEY, "vanityurl": vanity_name}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            result = data.get("response", {})
            if result.get("success") == 1:
                return result.get("steamid")
            return None
    except Exception as e:
        print(f"Steam vanity resolve error: {e}")
        return None

async def get_player_summaries(session, steam_ids):
    """Get player names/avatars for a list of Steam64 IDs."""
    url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2"
    params = {"key": STEAM_API_KEY, "steamids": ",".join(str(s) for s in steam_ids)}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            players = data.get("response", {}).get("players", [])
            return {p["steamid"]: p for p in players}
    except Exception as e:
        print(f"Steam summaries error: {e}")
        return {}

async def get_player_bans(session, steam_ids):
    """Check VAC/game bans for a list of Steam64 IDs. Returns list of ban objects."""
    url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerBans/v1"
    params = {"key": STEAM_API_KEY, "steamids": ",".join(str(s) for s in steam_ids)}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("players", [])
    except Exception as e:
        print(f"Steam ban check error: {e}")
        return []

async def get_cs2_hours(session, steam_id):
    """Get CS2 playtime in hours from Steam. Returns (hours_total, hours_2weeks) or (None, None)."""
    url = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1"
    params = {"key": STEAM_API_KEY, "steamid": steam_id, "appids_filter[0]": 730, "include_played_free_games": 1}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None, None
            data = await resp.json()
            games = data.get("response", {}).get("games", [])
            for g in games:
                if g.get("appid") == 730:
                    total   = round(g.get("playtime_forever", 0) / 60, 1)
                    recent  = round(g.get("playtime_2weeks", 0) / 60, 1)
                    return total, recent
            return 0, 0
    except Exception as e:
        print(f"CS2 hours error: {e}")
        return None, None

def parse_steam_url(url_or_id):
    """
    Parse a Steam profile URL or raw Steam64 ID into a steam64_id and optional vanity name.
    Returns (steam64_id_or_None, vanity_name_or_None)
    """
    import re
    url_or_id = url_or_id.strip()

    # Raw Steam64 ID
    if url_or_id.isdigit() and len(url_or_id) >= 15:
        return url_or_id, None

    # steamcommunity.com/profiles/76561198042824815
    m = re.search(r'steamcommunity\.com/profiles/(\d{15,})', url_or_id)
    if m:
        return m.group(1), None

    # steamcommunity.com/id/vanityname
    m = re.search(r'steamcommunity\.com/id/([^/?#]+)', url_or_id)
    if m:
        return None, m.group(1)

    return None, None

def build_ban_embed(banned_players, match_map, match_score):
    """Build an embed listing banned players found in a match lobby."""
    embed = discord.Embed(
        title=f"⚠️ Banned Player(s) Detected in Lobby",
        description=f"Match: **{map_display(match_map)}** ({match_score})",
        color=0xFF6B35,
        timestamp=datetime.now(timezone.utc)
    )

    for p in banned_players:
        name     = p.get("name", p.get("SteamId", "Unknown"))
        steam_id = p.get("SteamId", "")
        lines    = []

        if p.get("VACBanned"):
            n_vac = p.get("NumberOfVACBans", 1)
            days  = p.get("DaysSinceLastBan", 0)
            lines.append(f"🔴 **VAC Banned** — {n_vac} ban(s), last ban **{days} days ago**")

        game_bans = p.get("NumberOfGameBans", 0)
        if game_bans:
            days = p.get("DaysSinceLastBan", 0)
            lines.append(f"🟠 **Game Banned** — {game_bans} ban(s), last ban **{days} days ago**")

        if p.get("CommunityBanned"):
            lines.append("⚫ Community Banned")

        econ = p.get("EconomyBan", "none")
        if econ and econ != "none":
            lines.append(f"💸 Economy Ban: {econ}")

        profile_url = f"https://steamcommunity.com/profiles/{steam_id}"
        lines.append(f"[Steam Profile]({profile_url})")

        embed.add_field(
            name=f"👤 {name}",
            value="\n".join(lines),
            inline=False
        )

    embed.set_footer(text="Ban data via Steam Web API")
    return embed

# ============================================================
# CSFLOAT PRICE API
# ============================================================

CSFLOAT_BASE = "https://csfloat.com/api/v1"

# Wear name normalization
WEAR_MAP = {
    "fn": "Factory New", "factory new": "Factory New",
    "mw": "Minimal Wear", "minimal wear": "Minimal Wear",
    "ft": "Field-Tested", "field tested": "Field-Tested", "field-tested": "Field-Tested",
    "ww": "Well-Worn", "well worn": "Well-Worn", "well-worn": "Well-Worn",
    "bs": "Battle-Scarred", "battle scarred": "Battle-Scarred", "battle-scarred": "Battle-Scarred",
}

WEAR_FLOATS = {
    "Factory New":    (0.00, 0.07),
    "Minimal Wear":   (0.07, 0.15),
    "Field-Tested":   (0.15, 0.38),
    "Well-Worn":      (0.38, 0.45),
    "Battle-Scarred": (0.45, 1.00),
}

def normalize_market_hash_name(query):
    """
    Convert a natural language query like 'kukri safari mesh ft' or
    'stattrak ak47 redline field tested' into a CS2 market hash name.
    Returns (market_hash_name, is_stattrak, wear)
    """
    import re
    q = query.lower().strip()

    # Detect StatTrak — catch stattrak, stat trak, or standalone "st" anywhere
    is_st = False
    if re.search(r'\bstattrak\b|\bstat\s*trak\b', q):
        is_st = True
        q = re.sub(r'\bstat\s*trak\s*™?\s*|\bstattrak\s*™?\s*', '', q).strip()
    elif re.search(r'\bst\b', q):
        is_st = True
        q = re.sub(r'\bst\b', '', q).strip()

    # Detect wear — check end of string first
    wear = None
    for abbr, full in WEAR_MAP.items():
        # Match at end of string or as a standalone word
        pattern = r"\b" + re.escape(abbr) + r"\b"
        if re.search(pattern, q):
            wear = full
            q = re.sub(pattern, "", q).strip()
            break

    # Clean up extra spaces and dashes
    q = re.sub(r"\s+", " ", q).strip()

    # Common weapon name normalizations
    weapon_map = {
        "ak47": "AK-47", "ak 47": "AK-47", "ak-47": "AK-47",
        "m4a1s": "M4A1-S", "m4a1 s": "M4A1-S", "m4a1-s": "M4A1-S",
        "m4a4": "M4A4",
        "awp": "AWP",
        "deagle": "Desert Eagle", "desert eagle": "Desert Eagle",
        "usp": "USP-S", "usp-s": "USP-S", "usps": "USP-S",
        "glock": "Glock-18", "glock 18": "Glock-18", "glock-18": "Glock-18",
        "mp9": "MP9", "mp7": "MP7", "mp5": "MP5-SD",
        "famas": "FAMAS", "galil": "Galil AR", "galil ar": "Galil AR",
        "sg553": "SG 553", "sg 553": "SG 553",
        "aug": "AUG", "krieg": "SG 553",
        "nova": "Nova", "mag7": "MAG-7", "mag-7": "MAG-7",
        "xm1014": "XM1014", "sawedoff": "Sawed-Off", "sawed off": "Sawed-Off",
        "kukri": "Kukri Knife", "butterfly": "Butterfly Knife",
        "karambit": "Karambit", "m9": "M9 Bayonet", "bayonet": "Bayonet",
        "flip": "Flip Knife", "gut": "Gut Knife", "shadow": "Shadow Daggers",
        "huntsman": "Huntsman Knife", "falchion": "Falchion Knife",
        "bowie": "Bowie Knife", "stiletto": "Stiletto Knife",
        "talon": "Talon Knife", "navaja": "Navaja Knife",
        "skeleton": "Skeleton Knife", "paracord": "Paracord Knife",
        "survival": "Survival Knife", "nomad": "Nomad Knife",
        "classic": "Classic Knife", "ursus": "Ursus Knife",
        # Gloves
        "sport gloves": "Sport Gloves", "sport": "Sport Gloves",
        "specialist gloves": "Specialist Gloves", "specialist": "Specialist Gloves",
        "moto gloves": "Moto Gloves", "moto": "Moto Gloves",
        "hand wraps": "Hand Wraps", "handwraps": "Hand Wraps",
        "hydra gloves": "Hydra Gloves", "hydra": "Hydra Gloves",
        "bloodhound gloves": "Bloodhound Gloves", "bloodhound": "Bloodhound Gloves",
        "driver gloves": "Driver Gloves", "driver": "Driver Gloves",
        "broken fang gloves": "Broken Fang Gloves", "broken fang": "Broken Fang Gloves",
        "sports gloves": "Sport Gloves",
    }

    # Glove types that need ★ prefix
    glove_weapons = ["Sport Gloves", "Specialist Gloves", "Moto Gloves", "Hand Wraps",
                     "Hydra Gloves", "Bloodhound Gloves", "Driver Gloves", "Broken Fang Gloves"]

    # Try to find and replace weapon name
    result_weapon = None
    remaining = q
    for key, val in sorted(weapon_map.items(), key=lambda x: -len(x[0])):
        if q.startswith(key + " ") or q == key:
            result_weapon = val
            remaining = q[len(key):].strip()
            break
        # Also try weapon at end
        if q.endswith(" " + key) or q == key:
            result_weapon = val
            remaining = q[:q.rfind(key)].strip()
            break

    if not result_weapon:
        # Capitalize words as best guess
        parts = q.split()
        result_weapon = " ".join(w.capitalize() for w in parts[:2]) if parts else q
        remaining = " ".join(parts[2:]) if len(parts) > 2 else ""

    # Capitalize skin name
    skin_name = " ".join(w.capitalize() for w in remaining.split()) if remaining else None

    # Build market hash name
    if skin_name:
        base = f"{result_weapon} | {skin_name}"
    else:
        base = result_weapon

    # Add wear in parentheses
    if wear:
        market_hash_name = f"{base} ({wear})"
    else:
        market_hash_name = base

    # Add StatTrak prefix
    knife_types = ["Knife", "Karambit", "Bayonet", "Dagger", "Kukri"]
    is_knife = any(k in (result_weapon or "") for k in knife_types)
    is_glove = result_weapon in glove_weapons if result_weapon else False
    if is_st:
        if is_knife or is_glove:
            market_hash_name = f"★ StatTrak™ {market_hash_name}"
        else:
            market_hash_name = f"StatTrak™ {market_hash_name}"
    elif is_knife or is_glove:
        market_hash_name = f"★ {market_hash_name}"

    return market_hash_name, is_st, wear


async def fetch_csfloat_listings(session, market_hash_name, limit=10, min_float=None, max_float=None):
    """Fetch current listings for a skin from CSFloat."""
    from urllib.parse import urlencode
    # Build URL manually to ensure ★ and other special chars are encoded correctly
    param_dict = {
        "market_hash_name": market_hash_name,
        "sort_by": "lowest_price",
        "limit": limit,
    }
    if min_float is not None:
        param_dict["min_float"] = min_float
    if max_float is not None:
        param_dict["max_float"] = max_float
    base_params = urlencode(param_dict)
    url = f"{CSFLOAT_BASE}/listings?{base_params}"
    headers = {}
    if CSFLOAT_API_KEY and CSFLOAT_API_KEY != "YOUR_CSFLOAT_API_KEY":
        headers["Authorization"] = CSFLOAT_API_KEY
    try:
        async with session.get(url, headers=headers) as resp:
            print(f"CSFloat response: {resp.status}")
            if resp.status == 404:
                return None, "not_found"
            if resp.status == 401:
                return None, "auth_error"
            if resp.status != 200:
                text = await resp.text()
                print(f"CSFloat error body: {text[:200]}")
                return None, f"http_{resp.status}"
            data = await resp.json()
            if isinstance(data, list):
                data = {"data": data}
            listings = data.get("data", [])
            return listings, None
    except Exception as e:
        print(f"CSFloat price error: {e}")
        return None, "error"


def build_price_embed(market_hash_name, listings, query, min_float=None, max_float=None, steam_price=None):
    """Build a price check embed from CSFloat listings."""
    if not listings:
        embed = discord.Embed(
            title=f"💰 Price Check — Not Found",
            description=f"No listings found for `{market_hash_name}`\n\nDouble-check the weapon type and skin name. For gloves, include the full type e.g. `specialist gloves pillow punchers mw`.",
            color=0x888888,
            timestamp=datetime.now(timezone.utc)
        )
        return embed

    prices = [l["price"] for l in listings if l.get("price")]
    floats = [l["item"]["float_value"] for l in listings if l.get("item", {}).get("float_value")]
    scm_prices = [l["item"]["scm"]["price"] for l in listings if l.get("item", {}).get("scm", {}).get("price", 0) > 0]

    lowest   = min(prices) / 100 if prices else None
    highest  = max(prices) / 100 if prices else None
    median   = sorted(prices)[len(prices)//2] / 100 if prices else None
    avg      = sum(prices) / len(prices) / 100 if prices else None
    scm_med  = sorted(scm_prices)[len(scm_prices)//2] / 100 if scm_prices else None
    low_float  = min(floats) if floats else None
    high_float = max(floats) if floats else None

    # Get icon from first listing
    first_item = listings[0].get("item", {})
    icon_url = first_item.get("icon_url", "")
    collection = first_item.get("collection", "")
    description = first_item.get("description", "")
    # Strip italic HTML tags from description
    import re
    description = re.sub(r"<[^>]+>", "", description).strip()

    color = 0xF1C40F
    embed = discord.Embed(
        title=f"💰 {market_hash_name}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    # Set skin image as thumbnail
    if icon_url:
        # Try both CDN formats
        full_icon_url = f"https://community.cloudflare.steamstatic.com/economy/image/{icon_url}"
        embed.set_thumbnail(url=full_icon_url)

    # Collection and flavor text
    if collection or description:
        desc_parts = []
        if collection:
            desc_parts.append(f"*{collection}*")
        if description:
            # Just show the flavor text line (usually after \n\n)
            flavor = description.split("\n\n")[-1].strip() if "\n\n" in description else ""
            if flavor:
                desc_parts.append(f"*{flavor}*")
        embed.description = "\n".join(desc_parts)

    # CSFloat prices
    embed.add_field(
        name="💲 CSFloat Prices",
        value=(
            f"📉 Lowest   `${lowest:.2f}`\n"
            f"📊 Median   `${median:.2f}`\n"
            f"📈 Avg      `${avg:.2f}`\n"
            f"🔼 Highest  `${highest:.2f}`"
        ),
        inline=True
    )

    # Steam Market + float info

    market_lines = []
    if steam_price:
        if steam_price.get("lowest"):
            market_lines.append(f"🟩 Steam Low   `{steam_price['lowest']}`")
        if steam_price.get("median"):
            market_lines.append(f"🟢 Steam Med   `{steam_price['median']}`")
        if steam_price.get("volume"):
            market_lines.append(f"📦 24h Volume  `{steam_price['volume']}`")
    elif scm_med:
        market_lines.append(f"🟢 SCM Median `${scm_med:.2f}`")
    market_lines.append(f"🔢 CF Listings `{len(listings)}`")
    if low_float is not None:
        market_lines.append(f"🎯 Low Float  `{low_float:.4f}`")
        market_lines.append(f"📏 High Float `{high_float:.4f}`")
        if min_float or max_float:
            market_lines.append(f"🔍 Filter: `{min_float or 0:.2f}`–`{max_float or 1:.2f}`")
    embed.add_field(
        name="📊 Market Info",
        value="\n".join(market_lines),
        inline=True
    )

    # Top 3 cheapest with sticker callouts
    top3 = listings[:3]
    top3_lines = []
    for l in top3:
        p = l["price"] / 100
        item = l.get("item", {})
        fv = item.get("float_value", 0)
        st = "ST " if item.get("is_stattrak") else ""
        stickers = item.get("stickers", [])
        sticker_val = sum(s.get("scm", {}).get("price", 0) for s in stickers) / 100
        sticker_str = f" · 🏷️ Stickers `${sticker_val:.0f}`" if sticker_val > 1 else ""
        top3_lines.append(f"`${p:.2f}` · `{fv:.4f}` {st}{sticker_str}")
    if top3_lines:
        embed.add_field(name="🏷️ Cheapest Listings", value="\n".join(top3_lines), inline=False)

    from urllib.parse import quote
    csfloat_url = f"https://csfloat.com/search?market_hash_name={quote(market_hash_name)}"
    embed.add_field(name="\u200b", value=f"[View on CSFloat]({csfloat_url})", inline=False)
    embed.set_footer(text="Prices in USD · Data by CSFloat")
    return embed


async def fetch_steam_market_price(session, market_hash_name):
    """Fetch Steam Market price for a skin using the public price overview endpoint."""
    from urllib.parse import quote
    url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={quote(market_hash_name)}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if not data.get("success"):
                return None
            return {
                "lowest":   data.get("lowest_price"),
                "median":   data.get("median_price"),
                "volume":   data.get("volume"),
            }
    except Exception as e:
        print(f"Steam market price error: {e}")
        return None


# ============================================================
# STAT HELPERS
# ============================================================

def fmt(val, decimals=2, suffix=""):
    if val is None:
        return "-"
    if decimals == 0:
        return f"{int(round(val))}{suffix}"
    return f"{val:.{decimals}f}{suffix}"

def pct(val):
    if val is None:
        return "-"
    return f"{val * 100:.1f}%"

def rating_emoji(rating):
    if rating is None:
        return ""
    if rating >= 0.08:   return "🔥"   # +8 and above — great game
    if rating >= 0.03:   return "✅"   # +3 to +8 — solid above average
    if rating >= -0.03:  return "🔘"   # -3 to +3 — around average
    if rating >= -0.08:  return "📉"   # -8 to -3 — rough game
    return "💀"                         # below -8 — disaster

def map_display(map_name):
    if map_name and map_name.startswith("de_"):
        return map_name[3:].capitalize()
    return map_name or "Unknown"

def get_outcome(player_stats):
    """Derive win/loss/tie from rounds_won/rounds_lost."""
    won  = player_stats.get("rounds_won")
    lost = player_stats.get("rounds_lost")
    if won is None or lost is None:
        return "unknown"
    if won > lost:
        return "win"
    if lost > won:
        return "loss"
    return "tie"

def result_emoji(outcome):
    if outcome == "win":  return "🟢 WIN"
    if outcome == "loss": return "🔴 LOSS"
    if outcome == "tie":  return "🟡 TIE"
    return "⚪ ?"

def score_display(team_scores, player_team):
    """Format score as player_score-opponent_score."""
    if not team_scores:
        return "?-?"
    my_score = opp_score = None
    if player_team is not None:
        for ts in team_scores:
            if ts.get("team_number") == player_team:
                my_score = ts.get("score")
            else:
                opp_score = ts.get("score")
    # Fallback: if team matching failed, just show both scores
    if my_score is None or opp_score is None:
        scores = [ts.get("score", "?") for ts in team_scores]
        return "-".join(str(s) for s in scores)
    return f"{my_score}-{opp_score}"

# ============================================================
# EMBED BUILDER
# ============================================================

COMPETITIVE_RANK_NAMES = {
    0: "Unranked", 1: "Silver I", 2: "Silver II", 3: "Silver III",
    4: "Silver IV", 5: "Silver Elite", 6: "Silver Elite Master",
    7: "Gold Nova I", 8: "Gold Nova II", 9: "Gold Nova III", 10: "Gold Nova Master",
    11: "Master Guardian I", 12: "Master Guardian II", 13: "Master Guardian Elite",
    14: "Distinguished Master Guardian", 15: "Legendary Eagle",
    16: "Legendary Eagle Master", 17: "Supreme Master First Class",
    18: "Global Elite"
}

def rank_name(rank_int):
    if rank_int is None:
        return "—"
    return COMPETITIVE_RANK_NAMES.get(int(rank_int), f"Rank {rank_int}")

def build_profile_embed(profile, steam_summary=None, ban_data=None, cs2_hours=None, cs2_hours_recent=None):
    """Build a Discord embed for a player's career profile."""
    name     = profile.get("name", "Unknown")
    steam_id = profile.get("steam64_id", "")
    winrate  = profile.get("winrate")
    total    = profile.get("total_matches")
    privacy  = profile.get("privacy_mode", "unknown")
    first_match = profile.get("first_match_date", "")

    ranks  = profile.get("ranks") or {}
    rating = profile.get("rating") or {}
    stats  = profile.get("stats") or {}

    # Ranks — premier is a number, competitive is a list of {map_name, rank}
    premier  = ranks.get("premier")
    faceit   = ranks.get("faceit_elo")
    comp_list = ranks.get("competitive") or []

    # Overall Leetify rating lives in ranks, not rating
    overall_leetify = ranks.get("leetify")

    # Ratings — aim/util/pos are 0-100 scale; ct/t are small decimals, multiply by 100 for display
    aim_r     = rating.get("aim")
    util_r    = rating.get("utility")
    pos_r     = rating.get("positioning")
    clutch_r  = rating.get("clutch")
    opening_r = rating.get("opening")
    ct_leet   = rating.get("ct_leetify")
    t_leet    = rating.get("t_leetify")

    # Stats — note: accuracy fields are already percentages (0-100), reaction_time_ms is ms
    preaim_s    = stats.get("preaim")
    reaction_s  = stats.get("reaction_time_ms")
    accuracy_s  = stats.get("accuracy_enemy_spotted")   # already a % value
    cstrafe_s   = stats.get("counter_strafing_good_shots_ratio")  # already a %
    util_death  = stats.get("utility_on_death_avg")
    trade_win   = stats.get("trade_kills_success_percentage")
    traded_pct  = stats.get("traded_deaths_success_percentage")
    flash_hit   = stats.get("flashbang_hit_foe_per_flashbang")
    ct_open     = stats.get("ct_opening_duel_success_percentage")
    t_open      = stats.get("t_opening_duel_success_percentage")

    # First match date
    try:
        dt_first = datetime.fromisoformat(first_match.replace("Z", "+00:00"))
        first_str = dt_first.strftime("%b %d, %Y")
    except Exception:
        first_str = None

    embed = discord.Embed(
        title=f"📊  {name}",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    footer = f"Steam64: {steam_id}  |  Privacy: {privacy}"
    if first_str:
        footer += f"  |  On Leetify since {first_str}"
    footer += "  |  Data by Leetify"
    embed.set_footer(text=footer)

    # Overview
    embed.add_field(
        name="Overview",
        value=(
            f"Matches  `{fmt(total, 0)}`\n"
            f"Win Rate `{fmt(winrate * 100 if winrate else None, 1, '%')}`"
        ),
        inline=True
    )

    # Ranks
    rank_lines = []
    if premier:
        rank_lines.append(f"Premier `{int(premier):,}`")
    if faceit:
        rank_lines.append(f"FACEIT  `{int(faceit)} ELO`")
    if rank_lines:
        embed.add_field(name="Ranks", value="\n".join(rank_lines), inline=True)

    # Competitive ranks per map — sorted best to worst
    if comp_list:
        sorted_comp = sorted(comp_list, key=lambda x: x.get("rank", 0), reverse=True)
        comp_lines = [f"{map_display(m['map_name']):<10} `{rank_name(m['rank'])}`" for m in sorted_comp]
        embed.add_field(name="Competitive Ranks", value="\n".join(comp_lines), inline=False)

    # CT/T split — ct_leet/t_leet are small decimals, multiply by 100 to match Leetify UI
    ct_leet_disp = (ct_leet * 100) if ct_leet is not None else None
    t_leet_disp  = (t_leet  * 100) if t_leet  is not None else None
    embed.add_field(
        name="CT / T",
        value=(
            f"CT Rating `{fmt(ct_leet_disp, 2)}`\n"
            f"T  Rating `{fmt(t_leet_disp, 2)}`\n"
            f"CT Opens  `{fmt(ct_open, 1, '%')}`\n"
            f"T  Opens  `{fmt(t_open, 1, '%')}`"
        ),
        inline=True
    )

    # Leetify ratings — clutch/opening are small decimals, multiply by 100
    clutch_disp  = (clutch_r  * 100) if clutch_r  is not None else None
    opening_disp = (opening_r * 100) if opening_r is not None else None
    embed.add_field(
        name="Leetify Ratings",
        value=(
            f"Overall     `{fmt(overall_leetify, 2)}`\n"
            f"Aim         `{fmt(aim_r, 1)}`\n"
            f"Utility     `{fmt(util_r, 1)}`\n"
            f"Positioning `{fmt(pos_r, 1)}`\n"
            f"Clutch      `{fmt(clutch_disp, 2)}`\n"
            f"Opening     `{fmt(opening_disp, 2)}`"
        ),
        inline=True
    )

    # Aim stats — accuracy/cstrafe already percentages so display directly
    embed.add_field(
        name="Aim Stats",
        value=(
            f"Acc (spotted) `{fmt(accuracy_s, 1, '%')}`\n"
            f"Preaim        `{fmt(preaim_s, 1)}°`\n"
            f"Reaction      `{fmt(reaction_s, 0, 'ms')}`\n"
            f"C-Strafe      `{fmt(cstrafe_s, 1, '%')}`"
        ),
        inline=True
    )

    # Utility & trades
    embed.add_field(
        name="Utility & Trades",
        value=(
            f"Util on Death  `{fmt(util_death, 0)}`\n"
            f"Trade Kill%    `{fmt(trade_win, 1, '%')}`\n"
            f"Traded Death%  `{fmt(traded_pct, 1, '%')}`\n"
            f"Flash Hit/FB   `{fmt(flash_hit, 2)}`"
        ),
        inline=True
    )

    # Steam account info
    if steam_summary or ban_data or cs2_hours is not None:
        steam_lines = []

        # Account age
        if steam_summary:
            created = steam_summary.get("timecreated")
            visibility = steam_summary.get("communityvisibilitystate", 3)
            vis_str = "🔒 Private" if visibility == 1 else ("👥 Friends Only" if visibility == 2 else "🌐 Public")
            if created:
                age_days = (datetime.now(timezone.utc) - datetime.fromtimestamp(created, tz=timezone.utc)).days
                age_years = age_days // 365
                age_months = (age_days % 365) // 30
                if age_years > 0:
                    age_str = f"{age_years}y {age_months}m"
                else:
                    age_str = f"{age_months} months"
                steam_lines.append(f"📅 Account Age  `{age_str}`")
                if age_days < 180:
                    steam_lines.append(f"⚠️ **New account** ({age_days} days old)")
            steam_lines.append(f"👁️ Profile       `{vis_str}`")

        # CS2 hours
        if cs2_hours is not None:
            if cs2_hours == 0:
                steam_lines.append("🎮 CS2 Hours    `0h` *(or hidden — game details may be private)*")
            else:
                steam_lines.append(f"🎮 CS2 Hours    `{cs2_hours}h total`" + (f"  `{cs2_hours_recent}h (2wks)`" if cs2_hours_recent else ""))

        # Bans
        if ban_data:
            vac = ban_data.get("VACBanned", False)
            game_bans = ban_data.get("NumberOfGameBans", 0)
            days_since = ban_data.get("DaysSinceLastBan", 0)
            econ = ban_data.get("EconomyBan", "none")
            community = ban_data.get("CommunityBanned", False)

            if vac or game_bans or community or (econ and econ != "none"):
                n_vac = ban_data.get("NumberOfVACBans", 0)
                if vac:
                    steam_lines.append(f"🔴 **VAC Banned** — {n_vac} ban(s), last **{days_since}d ago**")
                if game_bans:
                    steam_lines.append(f"🟠 **Game Banned** — {game_bans} ban(s), last **{days_since}d ago**")
                if community:
                    steam_lines.append("⚫ Community Banned")
                if econ and econ != "none":
                    steam_lines.append(f"💸 Economy Ban: `{econ}`")
            else:
                steam_lines.append("✅ No VAC, game, or community bans")

        if steam_lines:
            embed.add_field(name="Steam", value="\n".join(steam_lines), inline=False)

    # Recent form — last 5 matches
    recent = profile.get("recent_matches") or []
    if recent:
        form_parts = []
        for m in recent[:5]:
            out = m.get("outcome", "")
            map_n = map_display(m.get("map_name", ""))
            score = m.get("score", [])
            score_str = f"{score[0]}-{score[1]}" if len(score) == 2 else "?"
            icon = "🟢" if out == "win" else ("🔴" if out == "loss" else "🟡")
            form_parts.append(f"{icon} {map_n} `{score_str}`")
        embed.add_field(name="Recent Form", value="\n".join(form_parts), inline=False)

    leetify_url = f"https://leetify.com/app/profile/{steam_id}"
    embed.add_field(name="\u200b", value=f"[View on Leetify]({leetify_url})", inline=False)

    return embed


def build_embed(player_name, match, player_stats, career=None):
    # Core match info
    map_name    = match.get("map_name", "")
    team_scores = match.get("team_scores", [])
    finished_at = match.get("finished_at", "")
    has_ban     = match.get("has_banned_player", False)
    player_team = player_stats.get("initial_team_number")

    # Outcome derived from rounds
    outcome = get_outcome(player_stats)
    score   = score_display(team_scores, player_team)
    map_str = map_display(map_name)

    # Leetify ratings
    overall_rating = player_stats.get("leetify_rating")
    ct_rating      = player_stats.get("ct_leetify_rating")
    t_rating       = player_stats.get("t_leetify_rating")

    # Core stats
    kills   = player_stats.get("total_kills")
    deaths  = player_stats.get("total_deaths")
    assists = player_stats.get("total_assists")
    adr     = player_stats.get("dpr")                  # damage per round
    hs_kills = player_stats.get("total_hs_kills")
    total_kills_safe = kills if kills and kills > 0 else None
    hs_pct  = (hs_kills / total_kills_safe) if (hs_kills is not None and total_kills_safe) else None
    kd      = player_stats.get("kd_ratio")
    rounds  = player_stats.get("rounds_count")
    won     = player_stats.get("rounds_won")
    lost    = player_stats.get("rounds_lost")

    # Aim stats
    accuracy     = player_stats.get("accuracy_enemy_spotted")  # spotted accuracy, more meaningful than raw
    preaim_val   = player_stats.get("preaim")
    reaction     = player_stats.get("reaction_time")
    reaction_ms  = int(reaction * 1000) if reaction is not None else None
    counter_strafe_ratio = player_stats.get("counter_strafing_shots_good_ratio")
    spray_acc    = player_stats.get("spray_accuracy")

    # Trade/duel stats
    trade_attempt_pct  = player_stats.get("trade_kill_attempts_percentage")
    trade_success_pct  = player_stats.get("trade_kills_success_percentage")
    traded_death_pct   = player_stats.get("traded_death_attempts_percentage")
    traded_death_success = player_stats.get("traded_deaths_success_percentage")

    # Utility stats
    util_on_death  = player_stats.get("utility_on_death_avg")
    flash_assist   = player_stats.get("flash_assist")
    he_dmg         = player_stats.get("he_foes_damage_avg")
    smokes         = player_stats.get("smoke_thrown")
    molotovs       = player_stats.get("molotov_thrown")
    flashbangs     = player_stats.get("flashbang_thrown")

    # Multi-kills
    mk2 = player_stats.get("multi2k", 0)
    mk3 = player_stats.get("multi3k", 0)
    mk4 = player_stats.get("multi4k", 0)
    mk5 = player_stats.get("multi5k", 0)

    # Timestamp — convert UTC to Central Time
    try:
        from datetime import timedelta
        dt_utc = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        month = dt_utc.month
        offset = timedelta(hours=-5) if 3 <= month <= 11 else timedelta(hours=-6)
        dt_ct = dt_utc + offset
        tz_label = "CDT" if 3 <= month <= 11 else "CST"
        time_str = dt_ct.strftime(f"%b %d, %Y %I:%M %p {tz_label}")
    except Exception:
        time_str = finished_at

    color = 0x57F287 if outcome == "win" else (0xED4245 if outcome == "loss" else 0xFEE75C)

    embed = discord.Embed(
        title=f"{result_emoji(outcome)}  {player_name}  |  {map_str}  ({score})",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    career_str = ""
    if career:
        cr = career.get("rating") or {}
        aim_r = cr.get("aim")
        util_r = cr.get("utility")
        pos_r = cr.get("positioning")
        if any(v is not None for v in [aim_r, util_r, pos_r]):
            career_str = f"  |  Career: Aim {fmt(aim_r,1)} · Util {fmt(util_r,1)} · Pos {fmt(pos_r,1)}"

    embed.set_footer(
        text=f"📅 {time_str}"
             + ("  ⚠️ Banned player in lobby" if has_ban else "")
             + career_str
             + "  |  Data by Leetify"
    )

    # Row 1 — Core
    mvps = player_stats.get("mvps")
    embed.add_field(name="K / D / A / MVP", value=f"`{fmt(kills,0)} / {fmt(deaths,0)} / {fmt(assists,0)} / {fmt(mvps,0)}`", inline=True)
    embed.add_field(name="ADR",       value=f"`{fmt(adr, 1)}`",  inline=True)
    embed.add_field(name="K/D  |  HS%", value=f"`{fmt(kd, 2)}  |  {pct(hs_pct)}`", inline=True)

    # Row 2 — Leetify ratings — all three are small decimals, multiply by 100 to match Leetify UI
    overall_disp = (overall_rating * 100) if overall_rating is not None else None
    ct_disp      = (ct_rating      * 100) if ct_rating      is not None else None
    t_disp       = (t_rating       * 100) if t_rating       is not None else None
    embed.add_field(
        name="Leetify Rating",
        value=(
            f"{rating_emoji(overall_rating)} `{fmt(overall_disp, 2)}` Overall\n"
            f"🔵 `{fmt(ct_disp, 2)}` CT\n"
            f"🟠 `{fmt(t_disp, 2)}` T"
        ),
        inline=True
    )

    # Row 2 — Aim
    embed.add_field(
        name="Aim",
        value=(
            f"🎯 Accuracy  `{pct(accuracy)}`\n"
            f"💦 Spray     `{pct(spray_acc)}`\n"
            f"👁️ Preaim    `{fmt(preaim_val, 1)}°`\n"
            f"⚡ Reaction  `{fmt(reaction_ms, 0, 'ms')}`\n"
            f"↔️ C-Strafe  `{pct(counter_strafe_ratio)}`"
        ),
        inline=True
    )

    # Row 2 — Rounds
    embed.add_field(
        name="Rounds",
        value=(
            f"🔢 Played `{fmt(rounds, 0)}`\n"
            f"✅ Won    `{fmt(won, 0)}`\n"
            f"❌ Lost   `{fmt(lost, 0)}`"
        ),
        inline=True
    )

    # Row 3 — Trades
    embed.add_field(
        name="Trades",
        value=(
            f"⚔️ Trade Att%   `{pct(trade_attempt_pct)}`\n"
            f"✔️ Trade Win%   `{pct(trade_success_pct)}`\n"
            f"💀 Traded Death `{pct(traded_death_pct)}`\n"
            f"🛡️ TD Success   `{pct(traded_death_success)}`"
        ),
        inline=True
    )

    # Row 3 — Utility
    embed.add_field(
        name="Utility",
        value=(
            f"💰 Util on Death `{fmt(util_on_death, 0)}`\n"
            f"💣 HE Dmg/round  `{fmt(he_dmg, 1)}`\n"
            f"⚡ Flash Assists `{fmt(flash_assist, 0)}`\n"
            f"💨 Smokes/Mols   `{fmt(smokes,0)}`/`{fmt(molotovs,0)}`"
        ),
        inline=True
    )

    # Row 3 — Multi-kills
    mk_parts = []
    if mk2: mk_parts.append(f"2K×{mk2}")
    if mk3: mk_parts.append(f"3K×{mk3}")
    if mk4: mk_parts.append(f"4K×{mk4}")
    if mk5: mk_parts.append(f"5K×{mk5}")
    mk_str = "  ".join(mk_parts) if mk_parts else "—"
    embed.add_field(
        name="Multi-kills",
        value=f"`{mk_str}`",
        inline=True
    )

    return embed

# ============================================================
# BOT + COMMAND TREE
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ============================================================
# SLASH COMMANDS
# ============================================================

@tree.command(name="setchannel", description="Set this channel as the CS2Bot summary and match post channel for this server")
async def cmd_setchannel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channels = load_channels()
    channel_id = interaction.channel_id
    if channel_id not in channels:
        channels[channel_id] = default_channel_settings(channel_id)
    else:
        channels[channel_id]["channel_id"] = channel_id
    save_channels(channels)
    await interaction.followup.send(
        f"✅ This channel is now registered for CS2Bot posts.\n"
        f"Match posts: ✅  Daily summary: ✅  Weekly summary: ✅\n"
        f"Use `/settings` to customize what gets posted here.",
        ephemeral=True
    )


@tree.command(name="addplayer", description="Add a player to match tracking")
@app_commands.describe(name="Display name for this player", steamid="Steam64 ID (17-digit number)")
async def cmd_addplayer(interaction: discord.Interaction, name: str, steamid: str):
    await interaction.response.defer(ephemeral=True)
    if not steamid.isdigit() or len(steamid) < 15:
        await interaction.followup.send("❌ That doesn't look like a valid Steam64 ID.", ephemeral=True)
        return
    players = load_players()
    existed = name in players
    players[name] = steamid
    save_players(players)
    verb = "Updated" if existed else "Added"
    await interaction.followup.send(f"✅ {verb} **{name}** (`{steamid}`) — tracking from next poll.", ephemeral=True)


@tree.command(name="removeplayer", description="Remove a player from match tracking")
@app_commands.describe(name="Display name of the player to remove")
async def cmd_removeplayer(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    players = load_players()
    if name not in players:
        names = ", ".join(f"**{n}**" for n in players) or "none"
        await interaction.followup.send(f"❌ No player named **{name}**. Tracked: {names}", ephemeral=True)
        return
    del players[name]
    save_players(players)
    await interaction.followup.send(f"✅ Removed **{name}** from tracking.", ephemeral=True)


@tree.command(name="listplayers", description="Show all currently tracked players")
async def cmd_listplayers(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    players = load_players()
    if not players:
        await interaction.followup.send("No players are currently being tracked.", ephemeral=True)
        return
    lines = [f"**{name}** — `{sid}`" for name, sid in players.items()]
    embed = discord.Embed(title="📋 Tracked Players", description="\n".join(lines), color=0x5865F2)
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="profile", description="Show a player's career profile and Leetify ratings")
@app_commands.describe(player="Tracked player name, Steam64 ID, or full Steam profile URL")
async def cmd_profile(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=False)  # visible to channel

    players = load_players()
    steam_id = None
    display  = None

    # 1. Check tracked player names first
    if player in players:
        steam_id = players[player]
        display  = player

    # 2. Try parsing as Steam URL or raw Steam64 ID
    if not steam_id:
        parsed_id, vanity = parse_steam_url(player)
        if parsed_id:
            steam_id = parsed_id
        elif vanity:
            # Resolve vanity URL via Steam API
            async with aiohttp.ClientSession() as session:
                steam_id = await resolve_vanity_url(session, vanity)
            if not steam_id:
                await interaction.followup.send(
                    f"❌ Couldn't resolve Steam vanity URL **{vanity}** — check the URL and try again.",
                    ephemeral=True
                )
                return

    if not steam_id:
        names = ", ".join(f"**{n}**" for n in players) or "none"
        await interaction.followup.send(
            f"❌ Couldn't parse **{player}** as a tracked name, Steam64 ID, or Steam URL.\nTracked players: {names}",
            ephemeral=True
        )
        return

    async with aiohttp.ClientSession() as session:
        # Fetch Leetify profile first, then Steam data concurrently
        profile = await fetch_profile(session, steam_id=steam_id)
        steam_summary, ban_data, cs2_hours, cs2_hours_recent = None, None, None, None
        if STEAM_API_KEY and STEAM_API_KEY != "YOUR_STEAM_API_KEY":
            try:
                summaries, ban_list, hours_result = await asyncio.gather(
                    get_player_summaries(session, [steam_id]),
                    get_player_bans(session, [steam_id]),
                    get_cs2_hours(session, steam_id)
                )
                steam_summary = summaries.get(str(steam_id)) if summaries else None
                ban_data      = ban_list[0] if ban_list else None
                cs2_hours, cs2_hours_recent = hours_result
            except Exception as e:
                print(f"Steam data fetch error: {e}")

    # Use Steam display name as fallback if we don't have a tracked name
    if not display:
        if profile:
            display = profile.get("name", steam_id)
        elif steam_summary:
            display = steam_summary.get("personaname", steam_id)
        else:
            display = steam_id

    if not profile:
        # Leetify failed — build a Steam-only embed with whatever we have
        embed = discord.Embed(
            title=f"📊  {display}",
            description="⚠️ No Leetify data — profile may be private or account not registered.",
            color=0x888888,
            timestamp=datetime.now(timezone.utc)
        )
        if steam_summary or ban_data or cs2_hours is not None:
            steam_lines = []
            if steam_summary:
                created = steam_summary.get("timecreated")
                visibility = steam_summary.get("communityvisibilitystate", 3)
                vis_str = "🔒 Private" if visibility == 1 else ("👥 Friends Only" if visibility == 2 else "🌐 Public")
                if created:
                    age_days = (datetime.now(timezone.utc) - datetime.fromtimestamp(created, tz=timezone.utc)).days
                    age_years = age_days // 365
                    age_months = (age_days % 365) // 30
                    age_str = f"{age_years}y {age_months}m" if age_years > 0 else f"{age_months} months"
                    steam_lines.append(f"📅 Account Age  `{age_str}`")
                    if age_days < 180:
                        steam_lines.append(f"⚠️ **New account** ({age_days} days old)")
                steam_lines.append(f"👁️ Profile       `{vis_str}`")
            if cs2_hours is not None:
                if cs2_hours == 0:
                    steam_lines.append("🎮 CS2 Hours    `0h` *(or hidden)*")
                else:
                    steam_lines.append(f"🎮 CS2 Hours    `{cs2_hours}h total`" + (f"  `{cs2_hours_recent}h (2wks)`" if cs2_hours_recent else ""))
            if ban_data:
                vac = ban_data.get("VACBanned", False)
                game_bans = ban_data.get("NumberOfGameBans", 0)
                days_since = ban_data.get("DaysSinceLastBan", 0)
                econ = ban_data.get("EconomyBan", "none")
                community = ban_data.get("CommunityBanned", False)
                if vac or game_bans or community or (econ and econ != "none"):
                    if vac:
                        steam_lines.append(f"🔴 **VAC Banned** — {ban_data.get('NumberOfVACBans', 0)} ban(s), last **{days_since}d ago**")
                    if game_bans:
                        steam_lines.append(f"🟠 **Game Banned** — {game_bans} ban(s), last **{days_since}d ago**")
                    if community:
                        steam_lines.append("⚫ Community Banned")
                    if econ and econ != "none":
                        steam_lines.append(f"💸 Economy Ban: `{econ}`")
                else:
                    steam_lines.append("✅ No VAC, game, or community bans")
            if steam_lines:
                embed.add_field(name="Steam", value="\n".join(steam_lines), inline=False)
            profile_url = f"https://steamcommunity.com/profiles/{steam_id}"
            embed.add_field(name="\u200b", value=f"[Steam Profile]({profile_url})", inline=False)
        await interaction.followup.send(embed=embed)
        return

    embed = build_profile_embed(profile, steam_summary=steam_summary, ban_data=ban_data, cs2_hours=cs2_hours, cs2_hours_recent=cs2_hours_recent)
    embed.title = f"📊  {display}"
    await interaction.followup.send(embed=embed)


@tree.command(name="lastscore", description="Post a player's most recent match summary right now")
@app_commands.describe(name="Display name of the player (leave blank for all)")
async def cmd_lastscore(interaction: discord.Interaction, name: str = None):
    await interaction.response.defer(ephemeral=True)
    players = load_players()

    if name and name not in players:
        names = ", ".join(f"**{n}**" for n in players) or "none"
        await interaction.followup.send(f"❌ No player named **{name}**. Tracked: {names}", ephemeral=True)
        return

    targets = {name: players[name]} if name else players
    channels = get_all_channels(bot)
    if not channels:
        await interaction.followup.send("❌ No channels configured. Use /setchannel first.", ephemeral=True)
        return

    posted = 0
    async with aiohttp.ClientSession() as session:
        for pname, steam_id in targets.items():
            try:
                matches = await fetch_recent_matches(session, steam_id)
                if not matches or not isinstance(matches, list) or len(matches) == 0:
                    continue
                latest = matches[0]
                stats_list = latest.get("stats", [])
                player_stats = next(
                    (s for s in stats_list if str(s.get("steam64_id", "")) == str(steam_id)),
                    stats_list[0] if stats_list else {}
                )
                career = await fetch_profile(session, steam_id=steam_id)
                embed = build_embed(pname, latest, player_stats, career=career)
                for ch in get_all_channels(bot):
                    await ch.send(embed=embed)
                posted += 1
            except Exception as e:
                print(f"lastscore error ({pname}): {e}")

    msg = f"✅ Posted {posted} match summar{'y' if posted == 1 else 'ies'}." if posted else "⚠️ Couldn't retrieve match data."
    await interaction.followup.send(msg, ephemeral=True)

# ============================================================
# POLL LOOP
# ============================================================

async def poll_loop():
    await bot.wait_until_ready()
    state = load_state()

    async with aiohttp.ClientSession() as session:
        while not bot.is_closed():
            players = load_players()
            for player_name, steam_id in players.items():
                await asyncio.sleep(0)  # yield to event loop before each player
                try:
                    matches = await fetch_recent_matches(session, steam_id)
                    if not matches or not isinstance(matches, list) or len(matches) == 0:
                        continue

                    latest   = matches[0]
                    match_id = latest.get("id")
                    if not match_id:
                        continue

                    if state.get(steam_id) == match_id:
                        continue  # nothing new

                    # First time seeing this player — seed state silently, don't post
                    if steam_id not in state:
                        state[steam_id] = match_id
                        save_state(state)
                        print(f"Seeded state for {player_name} at match {match_id}")
                        continue

                    stats_list = latest.get("stats", [])
                    player_stats = next(
                        (s for s in stats_list if str(s.get("steam64_id", "")) == str(steam_id)),
                        stats_list[0] if stats_list else {}
                    )

                    career = await fetch_profile(session, steam_id=steam_id)
                    embed = build_embed(player_name, latest, player_stats, career=career)
                    for ch in get_channels_for(bot, "match_posts"):
                        await ch.send(embed=embed)

                    # Ban check — scan all players in the lobby
                    if STEAM_API_KEY and STEAM_API_KEY != "YOUR_STEAM_API_KEY":
                        try:
                            all_stats = latest.get("stats", [])
                            lobby_ids = [str(s.get("steam64_id")) for s in all_stats if s.get("steam64_id")]
                            if lobby_ids:
                                ban_data = await get_player_bans(session, lobby_ids)
                                summaries = await get_player_summaries(session, lobby_ids)
                                flagged = []
                                for b in ban_data:
                                    if b.get("VACBanned") or b.get("NumberOfGameBans", 0) > 0 or b.get("CommunityBanned"):
                                        sid = b.get("SteamId", "")
                                        b["name"] = summaries.get(sid, {}).get("personaname", sid)
                                        flagged.append(b)
                                if flagged:
                                    score = score_display(latest.get("team_scores", []), player_stats.get("initial_team_number"))
                                    ban_embed = build_ban_embed(flagged, latest.get("map_name", ""), score)
                                    for ch in get_channels_for(bot, "match_posts"):
                                        await ch.send(embed=ban_embed)
                        except Exception as ban_err:
                            print(f"Ban check error: {ban_err}")

                    state[steam_id] = match_id
                    save_state(state)

                except Exception as e:
                    print(f"Poll error ({player_name}): {e}")

                await asyncio.sleep(0.5)
                await asyncio.sleep(0)  # yield to event loop

            await asyncio.sleep(POLL_INTERVAL)


# ============================================================
# SUMMARY HELPERS
# ============================================================

def safe_avg(values):
    """Average of a list, ignoring None values."""
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None

def aggregate_matches(matches, since_dt):
    """
    Filter matches to those finishing after since_dt and aggregate stats.
    Returns a dict of aggregated stats or None if no matches in window.
    """
    window = []
    for m in matches:
        try:
            finished = datetime.fromisoformat(m.get("finished_at", "").replace("Z", "+00:00"))
            if finished >= since_dt:
                window.append(m)
        except Exception:
            continue

    if not window:
        return None

    wins = losses = ties = 0
    ratings = []
    ct_ratings = []
    t_ratings = []
    adrs = []
    kds = []
    hs_pcts = []
    mvps_total = 0
    deaths_total = 0
    reaction_times = []
    util_on_deaths = []
    trade_pcts = []
    opening_wr = []
    flash_assists = []
    cstrafe_ratios = []
    acc_spotted = []

    best_match = None
    worst_match = None

    for m in window:
        outcome = get_outcome(m)
        if outcome == "win":    wins += 1
        elif outcome == "loss": losses += 1
        else:                   ties += 1

        r = m.get("leetify_rating")
        if r is not None:
            ratings.append(r)
            if best_match is None or r > best_match["rating"]:
                best_match = {"rating": r, "map": m.get("map_name",""), "team_scores": m.get("team_scores",[]), "team": m.get("initial_team_number"), "match": m}
            if worst_match is None or r < worst_match["rating"]:
                worst_match = {"rating": r, "map": m.get("map_name",""), "team_scores": m.get("team_scores",[]), "team": m.get("initial_team_number"), "match": m}

        ct_ratings.append(m.get("ct_leetify_rating"))
        t_ratings.append(m.get("t_leetify_rating"))

        adr = m.get("dpr")
        if adr is not None: adrs.append(adr)

        kd = m.get("kd_ratio")
        if kd is not None: kds.append(kd)

        kills = m.get("total_kills") or 0
        deaths = m.get("total_deaths") or 0
        hs = m.get("total_hs_kills") or 0
        deaths_total += deaths
        mvps_total += m.get("mvps") or 0

        if kills > 0: hs_pcts.append(hs / kills)

        rt = m.get("reaction_time")
        if rt is not None: reaction_times.append(rt * 1000)

        util_on_deaths.append(m.get("utility_on_death_avg"))
        trade_pcts.append(m.get("trade_kills_success_percentage"))
        opening_wr.append(m.get("trade_kill_attempts_percentage"))
        flash_assists.append(m.get("flash_assist"))
        cstrafe_ratios.append(m.get("counter_strafing_shots_good_ratio"))
        acc_spotted.append(m.get("accuracy_enemy_spotted"))

    return {
        "count": len(window),
        "wins": wins, "losses": losses, "ties": ties,
        "avg_rating": safe_avg(ratings),
        "avg_ct_rating": safe_avg(ct_ratings),
        "avg_t_rating": safe_avg(t_ratings),
        "avg_adr": safe_avg(adrs),
        "avg_kd": safe_avg(kds),
        "avg_hs_pct": safe_avg(hs_pcts),
        "total_mvps": mvps_total,
        "total_deaths": deaths_total,
        "avg_reaction_ms": safe_avg(reaction_times),
        "avg_util_on_death": safe_avg(util_on_deaths),
        "avg_trade_pct": safe_avg(trade_pcts),
        "avg_opening_wr": safe_avg(opening_wr),
        "avg_flash_assists": safe_avg(flash_assists),
        "avg_cstrafe": safe_avg(cstrafe_ratios),
        "avg_acc_spotted": safe_avg(acc_spotted),
        "best_match": best_match,
        "worst_match": worst_match,
    }

def wildcard_stat(agg):
    """Pick the most interesting wildcard stat from the pool."""
    import random
    pool = [
        ("⚡ Reaction Time",   f"{fmt(agg.get('avg_reaction_ms'), 0, 'ms')}",   agg.get("avg_reaction_ms")),
        ("💰 Util on Death",   f"{fmt(agg.get('avg_util_on_death'), 0)}",        agg.get("avg_util_on_death")),
        ("⚔️ Trade Kill%",     f"{fmt((agg.get('avg_trade_pct') or 0)*100, 1, '%')}", agg.get("avg_trade_pct")),
        ("🤜 Opening Duel WR", f"{fmt((agg.get('avg_opening_wr') or 0)*100, 1, '%')}", agg.get("avg_opening_wr")),
        ("💡 Flash Assists",   f"{fmt(agg.get('avg_flash_assists'), 1)}",         agg.get("avg_flash_assists")),
        ("↔️ C-Strafe%",       f"{fmt((agg.get('avg_cstrafe') or 0)*100, 1, '%')}", agg.get("avg_cstrafe")),
    ]
    available = [(label, val_str, val) for label, val_str, val in pool if val is not None]
    if not available:
        return None, None
    label, val_str, _ = random.choice(available)
    return label, val_str


async def fetch_all_player_matches(session, players):
    """Fetch recent matches for all tracked players. Returns {name: [matches]}."""
    results = {}
    for name, steam_id in players.items():
        if steam_id in LEETIFY_404_CACHE:
            continue
        matches = await fetch_recent_matches(session, steam_id)
        if matches and isinstance(matches, list):
            # Flatten stats into each match for easier aggregation
            flat = []
            for m in matches:
                stats_list = m.get("stats", [])
                player_stats = next(
                    (s for s in stats_list if str(s.get("steam64_id", "")) == str(steam_id)),
                    None
                )
                if player_stats:
                    merged = {**m, **player_stats}
                    flat.append(merged)
            if flat:
                results[name] = flat
        await asyncio.sleep(0.5)
        await asyncio.sleep(0)  # yield to event loop
    return results


def build_daily_embed(player_data, post_date):
    """Build daily summary embed from {name: agg} dict."""
    total_matches = sum(d["count"] for d in player_data.values())
    DIVIDER = "──────────────────"

    # Build player lines as a single description block
    player_lines = []
    for name, agg in sorted(player_data.items(), key=lambda x: x[1].get("avg_rating") or -99, reverse=True):
        r    = agg.get("avg_rating")
        best  = agg.get("best_match")
        worst = agg.get("worst_match")
        count = agg["count"]

        best_str = worst_str = ""
        if best:
            bs = score_display(best["match"].get("team_scores", []), best["match"].get("initial_team_number"))
            best_str = f"  🏆 `{fmt((best['rating']*100), 2)}` {map_display(best['map'])} ({bs})"
        if worst:
            ws = score_display(worst["match"].get("team_scores", []), worst["match"].get("initial_team_number"))
            worst_str = f"  📉 `{fmt((worst['rating']*100), 2)}` {map_display(worst['map'])} ({ws})"

        r_disp = fmt((r * 100) if r else None, 2)
        player_lines.append(
            f"**{name}** · {count} match{'es' if count != 1 else ''} · "
            f"{agg['wins']}W-{agg['losses']}L-{agg['ties']}T\n"
            f"{rating_emoji(r)} `{r_disp}` · "
            f"ADR `{fmt(agg.get('avg_adr'), 1)}` · "
            f"K/D `{fmt(agg.get('avg_kd'), 2)}` · "
            f"HS% `{fmt((agg.get('avg_hs_pct') or 0) * 100, 1, '%')}` · "
            f"MVPs `{agg['total_mvps']}`\n"
            f"{best_str}{worst_str}"
        )

    # Moments of the day
    moments = []
    if player_data:
        all_aggs = list(player_data.items())

        best_rating_name, best_rating_agg = max(all_aggs, key=lambda x: x[1].get("best_match", {}).get("rating") or -99)
        bm = best_rating_agg.get("best_match")
        if bm:
            bs = score_display(bm["match"].get("team_scores", []), bm["match"].get("initial_team_number"))
            moments.append(f"🏆 Best Match: **{best_rating_name}** `{fmt(bm['rating']*100, 2)}` on {map_display(bm['map'])} ({bs})")

        best_adr_name, best_adr_agg = max(all_aggs, key=lambda x: x[1].get("avg_adr") or 0)
        moments.append(f"💣 Best ADR: **{best_adr_name}** `{fmt(best_adr_agg.get('avg_adr'), 1)}`")

        best_acc_name, best_acc_agg = max(all_aggs, key=lambda x: x[1].get("avg_acc_spotted") or 0)
        moments.append(f"🎯 Best Accuracy: **{best_acc_name}** `{fmt(best_acc_agg.get('avg_acc_spotted'), 1, '%')}`")

        best_hs_name, best_hs_agg = max(all_aggs, key=lambda x: (x[1].get("avg_hs_pct") or 0))
        moments.append(f"🔫 Best HS%: **{best_hs_name}** `{fmt((best_hs_agg.get('avg_hs_pct') or 0)*100, 1, '%')}`")

        best_mvp_name, best_mvp_agg = max(all_aggs, key=lambda x: x[1].get("total_mvps") or 0)
        moments.append(f"⭐ Most MVPs: **{best_mvp_name}** `{best_mvp_agg['total_mvps']}`")

        import time
        day_num = int(time.time()) // 86400
        wc_pool = [
            ("⚡ Reaction Time",   "avg_reaction_ms",    lambda v: fmt(v, 0, "ms"),            lambda v: v or 99999),
            ("💰 Util on Death",   "avg_util_on_death",  lambda v: fmt(v, 0),                  lambda v: v or 99999),
            ("⚔️ Trade Kill%",     "avg_trade_pct",      lambda v: fmt((v or 0)*100, 1, "%"),  lambda v: -(v or 0)),
            ("🤜 Opening Duel WR", "avg_opening_wr",     lambda v: fmt((v or 0)*100, 1, "%"),  lambda v: -(v or 0)),
            ("💡 Flash Assists",   "avg_flash_assists",  lambda v: fmt(v, 1),                  lambda v: -(v or 0)),
            ("↔️ C-Strafe%",       "avg_cstrafe",        lambda v: fmt((v or 0)*100, 1, "%"),  lambda v: -(v or 0)),
        ]
        wc_label, wc_key, wc_fmt, wc_sort = wc_pool[day_num % len(wc_pool)]
        best_wc = min(all_aggs, key=lambda x: wc_sort(x[1].get(wc_key)))
        wc_val = wc_fmt(best_wc[1].get(wc_key))
        moments.append(f"🎲 {wc_label}: **{best_wc[0]}** `{wc_val}`")

    description = (
        f"**{total_matches}** matches · {len(player_data)} player(s)\n"
        f"{DIVIDER}\n"
        + f"\n{DIVIDER}\n".join(player_lines)
        + f"\n{DIVIDER}\n"
        + "\n".join(moments)
    )

    embed = discord.Embed(
        title=f"📅 Daily Summary — {post_date.strftime('%A, %b %d')}",
        description=description,
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Data by Leetify  |  CS2Bot Daily Summary")
    return embed


def build_weekly_embed(player_data, week_start, week_end):
    """Build weekly summary embed from {name: agg} dict."""
    qualified = {n: a for n, a in player_data.items() if a["count"] >= 3}
    all_players = player_data  # for awards that include everyone

    total_matches = sum(d["count"] for d in player_data.values())

    embed = discord.Embed(
        title=f"📊 Weekly Summary — {week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}",
        description=f"**{total_matches}** matches played · Leaderboard requires 3+ matches",
        color=0xF1C40F,
        timestamp=datetime.now(timezone.utc)
    )

    # Leaderboard
    if qualified:
        rows = []
        for name, agg in sorted(qualified.items(), key=lambda x: x[1].get("avg_rating") or -99, reverse=True):
            r = agg.get("avg_rating")
            ct_r = agg.get("avg_ct_rating")
            t_r = agg.get("avg_t_rating")
            rows.append(
                f"**{name}** — {agg['wins']}W-{agg['losses']}L-{agg['ties']}T · "
                f"{rating_emoji(r)}`{fmt((r*100) if r else None, 2)}` · "
                f"ADR `{fmt(agg.get('avg_adr'),1)}` · "
                f"K/D `{fmt(agg.get('avg_kd'),2)}` · "
                f"HS% `{fmt((agg.get('avg_hs_pct') or 0)*100,1,'%')}`\n"
                f"CT `{fmt((ct_r*100) if ct_r else None,2)}` · T `{fmt((t_r*100) if t_r else None,2)}`"
            )
        embed.add_field(name="🏅 Leaderboard", value="\n\n".join(rows), inline=False)
    else:
        embed.add_field(name="🏅 Leaderboard", value="No players met the 3-match minimum this week.", inline=False)

    # Weekly awards — use all players not just qualified
    if all_players:
        awards = []
        agg_list = list(all_players.items())

        best_r = max(agg_list, key=lambda x: x[1].get("avg_rating") or -99)
        awards.append(f"🏆 Best Avg Rating: **{best_r[0]}** `{fmt((best_r[1].get('avg_rating') or 0)*100, 2)}`")

        most_played = max(agg_list, key=lambda x: x[1]["count"])
        awards.append(f"📈 Most Matches: **{most_played[0]}** `{most_played[1]['count']}`")

        best_wr = max(agg_list, key=lambda x: x[1]["wins"] / max(x[1]["count"], 1))
        wr_pct = best_wr[1]["wins"] / max(best_wr[1]["count"], 1) * 100
        awards.append(f"🔥 Best Win Rate: **{best_wr[0]}** `{wr_pct:.0f}%` ({best_wr[1]['wins']}W-{best_wr[1]['losses']}L)")

        best_adr = max(agg_list, key=lambda x: x[1].get("avg_adr") or 0)
        awards.append(f"🎯 Best Avg ADR: **{best_adr[0]}** `{fmt(best_adr[1].get('avg_adr'), 1)}`")

        most_deaths = max(agg_list, key=lambda x: x[1]["total_deaths"])
        awards.append(f"💀 Most Deaths: **{most_deaths[0]}** `{most_deaths[1]['total_deaths']}`")

        best_kd = max(agg_list, key=lambda x: x[1].get("avg_kd") or 0)
        awards.append(f"🧠 Best Avg K/D: **{best_kd[0]}** `{fmt(best_kd[1].get('avg_kd'), 2)}`")

        most_mvps = max(agg_list, key=lambda x: x[1]["total_mvps"])
        awards.append(f"⭐ Most MVPs: **{most_mvps[0]}** `{most_mvps[1]['total_mvps']}`")

        best_hs = max(agg_list, key=lambda x: x[1].get("avg_hs_pct") or 0)
        awards.append(f"🔫 Best Avg HS%: **{best_hs[0]}** `{fmt((best_hs[1].get('avg_hs_pct') or 0)*100, 1, '%')}`")

        # Wildcard — pick a consistent stat for the week using a seeded label
        # Use a fixed pool entry based on current week number for consistency
        import random, time
        week_num = int(time.time()) // (7 * 86400)
        wc_pool = [
            ("⚡ Reaction Time",   "avg_reaction_ms",    lambda v: fmt(v, 0, "ms"),          lambda v: v or 99999,   True),   # lower is better
            ("💰 Util on Death",   "avg_util_on_death",  lambda v: fmt(v, 0),                lambda v: v or 99999,   True),   # lower is better
            ("⚔️ Trade Kill%",     "avg_trade_pct",      lambda v: fmt((v or 0)*100, 1, "%"), lambda v: -(v or 0),   False),
            ("🤜 Opening Duel WR", "avg_opening_wr",     lambda v: fmt((v or 0)*100, 1, "%"), lambda v: -(v or 0),   False),
            ("💡 Flash Assists",   "avg_flash_assists",  lambda v: fmt(v, 1),                 lambda v: -(v or 0),   False),
            ("↔️ C-Strafe%",       "avg_cstrafe",        lambda v: fmt((v or 0)*100, 1, "%"), lambda v: -(v or 0),   False),
        ]
        wc_label, wc_key, wc_fmt, wc_sort, wc_lower = wc_pool[week_num % len(wc_pool)]
        best_wc = min(agg_list, key=lambda x: wc_sort(x[1].get(wc_key)))
        wc_val = wc_fmt(best_wc[1].get(wc_key))
        awards.append(f"🎲 {wc_label}: **{best_wc[0]}** `{wc_val}`")

        embed.add_field(name="🎖️ Weekly Awards", value="\n".join(awards), inline=False)

    embed.set_footer(text="Data by Leetify  |  CS2Bot Weekly Summary")
    return embed



@tree.command(name="summary", description="Post today's match summary right now")
async def cmd_summary(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset = timedelta(hours=-5) if 3 <= month <= 11 else timedelta(hours=-6)
    now_ct = now_utc + offset
    since_ct_midnight = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = since_ct_midnight - offset

    players = load_players()
    channels = get_channels_for(bot, "daily_summary")
    if not channels:
        await interaction.followup.send("❌ No channels configured for daily summaries. Use /setchannel first.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        all_matches = await fetch_all_player_matches(session, players)

    player_data = {}
    for name, matches in all_matches.items():
        agg = aggregate_matches(matches, since_utc)
        if agg:
            player_data[name] = agg

    if not player_data:
        await interaction.followup.send("No matches found for today.", ephemeral=True)
        return

    embed = build_daily_embed(player_data, now_ct)
    for ch in channels:
        await ch.send(embed=embed)
    await interaction.followup.send("✅ Daily summary posted.", ephemeral=True)


@tree.command(name="weeklysummary", description="Post this week's summary right now")
async def cmd_weeklysummary(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset = timedelta(hours=-5) if 3 <= month <= 11 else timedelta(hours=-6)
    now_ct = now_utc + offset
    week_end_ct   = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_ct = week_end_ct - timedelta(days=7)
    since_utc     = week_start_ct - offset

    players = load_players()
    channels = get_channels_for(bot, "weekly_summary")
    if not channels:
        await interaction.followup.send("❌ No channels configured for weekly summaries. Use /setchannel first.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        all_matches = await fetch_all_player_matches(session, players)

    player_data = {}
    for name, matches in all_matches.items():
        agg = aggregate_matches(matches, since_utc)
        if agg:
            player_data[name] = agg

    if not player_data:
        await interaction.followup.send("No matches found for this week.", ephemeral=True)
        return

    embed = build_weekly_embed(player_data, week_start_ct, week_end_ct)
    for ch in channels:
        await ch.send(embed=embed)
    await interaction.followup.send("✅ Weekly summary posted.", ephemeral=True)

@tree.command(name="price", description="Check current CSFloat prices for a CS2 skin")
@app_commands.describe(
    query="Skin name and wear, e.g. 'kukri safari mesh ft' or 'stattrak ak47 redline mw'",
    min_float="Minimum float value (optional, e.g. 0.15)",
    max_float="Maximum float value (optional, e.g. 0.20)"
)
async def cmd_price(interaction: discord.Interaction, query: str, min_float: float = None, max_float: float = None):
    await interaction.response.defer(ephemeral=False)

    if not CSFLOAT_API_KEY or CSFLOAT_API_KEY == "YOUR_CSFLOAT_API_KEY":
        await interaction.followup.send("❌ CSFloat API key not configured.", ephemeral=True)
        return

    try:
        market_hash_name, is_st, wear = normalize_market_hash_name(query)
        print(f"Price command: query={query!r} -> market_hash_name={market_hash_name!r}")
    except Exception as e:
        print(f"normalize error: {e}")
        await interaction.followup.send(f"❌ Error parsing query: {e}", ephemeral=True)
        return

    # Try with ★ first, fall back to without if empty
    api_hash_name = market_hash_name  # keep ★ for initial attempt
    print(f"API hash name: {api_hash_name!r}")

    async with aiohttp.ClientSession() as session:
        listings, error = await fetch_csfloat_listings(session, api_hash_name, min_float=min_float, max_float=max_float)
        # Steam Market uses ★ prefix for knives/gloves — use market_hash_name directly
        steam_price = await fetch_steam_market_price(session, market_hash_name)


    # If no listings with ★, try without it
    if (not listings or len(listings) == 0) and "★" in api_hash_name:
        alt_name = api_hash_name.replace("★ ", "").replace("★", "").strip()
        print(f"Retrying without ★: {alt_name}")
        async with aiohttp.ClientSession() as session:
            listings, error = await fetch_csfloat_listings(session, alt_name, min_float=min_float, max_float=max_float)
        if listings:
            market_hash_name = alt_name
            api_hash_name = alt_name

    if error == "not_found":
        # Fuzzy fallback — build a simpler name directly from the query
        # Strip wear abbreviations and try capitalizing remaining words
        import re
        fallback_q = query.lower()
        fallback_q = re.sub(r'\b(fn|mw|ft|ww|bs|factory new|minimal wear|field.?tested|well.?worn|battle.?scarred|stattrak|stat trak|\bst\b)\b', '', fallback_q).strip()
        fallback_q = re.sub(r'\s+', ' ', fallback_q).strip()
        if wear:
            fallback_name = " ".join(w.capitalize() for w in fallback_q.split()) + f" ({wear})"
        else:
            fallback_name = " ".join(w.capitalize() for w in fallback_q.split())
        if is_st:
            fallback_name = f"★ StatTrak™ {fallback_name}"
        elif fallback_name != market_hash_name:
            pass  # try as-is
        async with aiohttp.ClientSession() as session:
            listings, error = await fetch_csfloat_listings(session, fallback_name)
        if listings is not None:
            market_hash_name = fallback_name

    if error == "auth_error":
        await interaction.followup.send("❌ CSFloat API key is invalid or expired.", ephemeral=True)
        return

    embed = build_price_embed(market_hash_name, listings or [], query, min_float=min_float, max_float=max_float, steam_price=steam_price)
    await interaction.followup.send(embed=embed)


@tree.command(name="settings", description="View or change CS2Bot settings for this channel")
@app_commands.describe(
    setting="Which setting to change (leave blank to view current settings)",
    value="Turn the setting on or off"
)
@app_commands.choices(
    setting=[
        app_commands.Choice(name="match_posts", value="match_posts"),
        app_commands.Choice(name="daily_summary", value="daily_summary"),
        app_commands.Choice(name="weekly_summary", value="weekly_summary"),
    ],
    value=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def cmd_settings(interaction: discord.Interaction, setting: str = None, value: str = None):
    await interaction.response.defer(ephemeral=True)
    channels = load_channels()
    channel_id = interaction.channel_id

    # Auto-register channel if not yet set up
    if channel_id not in channels:
        channels[channel_id] = default_channel_settings(channel_id)
        save_channels(channels)

    cfg = channels[channel_id]

    # Show current settings if no args
    if not setting:
        def status(key):
            return "✅ On" if cfg.get(key, True) else "❌ Off"
        embed = discord.Embed(
            title="⚙️ CS2Bot Settings — This Channel",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Settings",
            value=(
                f"Match Posts    {status('match_posts')}\n"
                f"Daily Summary  {status('daily_summary')}\n"
                f"Weekly Summary {status('weekly_summary')}"
            ),
            inline=False
        )
        embed.set_footer(text="Use /settings [setting] [on/off] to change")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Validate
    valid_settings = ["match_posts", "daily_summary", "weekly_summary"]
    if setting not in valid_settings:
        await interaction.followup.send(f"❌ Unknown setting. Choose from: {', '.join(valid_settings)}", ephemeral=True)
        return

    if not value:
        current = "on" if cfg.get(setting, True) else "off"
        await interaction.followup.send(f"ℹ️ **{setting}** is currently **{current}**. Pass `on` or `off` to change it.", ephemeral=True)
        return

    new_val = value == "on"
    channels[channel_id][setting] = new_val
    save_channels(channels)

    label_map = {
        "match_posts": "Match Posts",
        "daily_summary": "Daily Summary",
        "weekly_summary": "Weekly Summary",
    }
    status_str = "✅ enabled" if new_val else "❌ disabled"
    await interaction.followup.send(f"{status_str} **{label_map[setting]}** for this channel.", ephemeral=True)


# ============================================================
# SUMMARY SCHEDULER
# ============================================================

async def summary_loop():
    """Background task that posts daily and weekly summaries at scheduled times."""
    await bot.wait_until_ready()
    last_daily  = None
    last_weekly = None

    while not bot.is_closed():
        now_utc = datetime.now(timezone.utc)

        # Convert to CDT (UTC-5 May-Nov, UTC-6 otherwise)
        from datetime import timedelta
        month = now_utc.month
        offset = timedelta(hours=-5) if 3 <= month <= 11 else timedelta(hours=-6)
        now_ct = now_utc + offset
        today_date = now_ct.date()

        # Daily — post at midnight CDT
        if now_ct.hour == 0 and now_ct.minute < 5:
            if last_daily != today_date:
                try:
                    since = (now_utc - timedelta(hours=24)).replace(hour=0, minute=0, second=0, microsecond=0)
                    # Adjust since to previous midnight CDT
                    since_ct_midnight = (now_ct - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    since_utc = since_ct_midnight - offset

                    players = load_players()
                    async with aiohttp.ClientSession() as session:
                        all_matches = await fetch_all_player_matches(session, players)

                    player_data = {}
                    for name, matches in all_matches.items():
                        agg = aggregate_matches(matches, since_utc)
                        if agg:
                            player_data[name] = agg

                    if player_data:
                        embed = build_daily_embed(player_data, now_ct)
                        for ch in get_channels_for(bot, "daily_summary"):
                            await ch.send(embed=embed)
                        print(f"Daily summary posted for {today_date}")

                    last_daily = today_date
                except Exception as e:
                    print(f"Daily summary error: {e}")

        # Weekly — post Sunday midnight CDT (weekday 6 = Sunday)
        if now_ct.weekday() == 6 and now_ct.hour == 0 and now_ct.minute < 5:
            if last_weekly != today_date:
                try:
                    week_end_ct   = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
                    week_start_ct = week_end_ct - timedelta(days=7)
                    since_utc     = week_start_ct - offset

                    players = load_players()
                    async with aiohttp.ClientSession() as session:
                        all_matches = await fetch_all_player_matches(session, players)

                    player_data = {}
                    for name, matches in all_matches.items():
                        agg = aggregate_matches(matches, since_utc)
                        if agg:
                            player_data[name] = agg

                    if player_data:
                        embed = build_weekly_embed(player_data, week_start_ct, week_end_ct)
                        for ch in get_channels_for(bot, "weekly_summary"):
                            await ch.send(embed=embed)
                        print(f"Weekly summary posted for week ending {today_date}")

                    last_weekly = today_date
                except Exception as e:
                    print(f"Weekly summary error: {e}")

        await asyncio.sleep(60)  # check every minute

# ============================================================
# STARTUP
# ============================================================

@bot.event
async def on_ready():
    await tree.sync()
    print(f"CS2Bot online as {bot.user} — slash commands synced")
    bot.loop.create_task(poll_loop())
    bot.loop.create_task(summary_loop())

bot.run(DISCORD_TOKEN)
