import hashlib
import hmac
import html
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from config import settings
from database.database import async_session_factory
from database.models import Bot, BotClick, Category, User, UserFavorite
from database.repositories import (
    BotRepository,
    CategoryRepository,
    FavoriteRepository,
    UserRepository,
)

logger = logging.getLogger("webapp_server")

WEBAPP_DIR = Path(__file__).parent / "webapp"

# In-memory avatar cache: username -> {"data": bytes | None, "content_type": str, "expires_at": float}
AVATAR_CACHE: Dict[str, Dict[str, Any]] = {}
AVATAR_CACHE_TTL = 7 * 86400  # 7 days for valid avatars
AVATAR_NEGATIVE_TTL = 3600    # 1 hour for missing avatars


def generate_fallback_svg(bot_emoji: str = "🤖", bot_name: str = "Bot") -> bytes:
    """Generate a clean, high-resolution SVG icon fallback for a bot."""
    escaped_emoji = html.escape(bot_emoji or "🤖")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1e293b"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="96" height="96" rx="22" fill="url(#bg)" stroke="rgba(255,255,255,0.08)" stroke-width="2"/>
  <text x="50%" y="54%" dominant-baseline="central" text-anchor="middle" font-size="38">{escaped_emoji}</text>
</svg>"""
    return svg.encode("utf-8")


async def fetch_telegram_avatar(username: str, bot_token: str) -> Optional[Tuple[bytes, str]]:
    """
    Safely retrieve Telegram profile avatar for a bot using Telegram Bot API.
    Caches results in memory to avoid repeated external requests.
    BOT_TOKEN is strictly kept on the server and NEVER exposed to frontend.
    """
    clean_username = username.strip().lstrip("@")
    if not clean_username or not bot_token or bot_token == "1234567890:ABCDefghIJKlmnoPQRstuvWXYZ123456789":
        return None

    now = time.time()
    cache_key = clean_username.lower()
    cached = AVATAR_CACHE.get(cache_key)
    if cached and cached["expires_at"] > now:
        if cached["data"]:
            return cached["data"], cached["content_type"]
        return None

    api_url = f"https://api.telegram.org/bot{bot_token}"
    try:
        timeout = aiohttp.ClientTimeout(total=4.0)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            # 1. getChat to obtain photo metadata
            chat_res = await client.get(f"{api_url}/getChat", params={"chat_id": f"@{clean_username}"})
            if chat_res.status != 200:
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            chat_data = await chat_res.json()
            if not chat_data.get("ok"):
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            result = chat_data.get("result", {})
            photo = result.get("photo")
            if not photo:
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            file_id = photo.get("small_file_id") or photo.get("big_file_id")
            if not file_id:
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            # 2. getFile to obtain downloadable file_path
            file_res = await client.get(f"{api_url}/getFile", params={"file_id": file_id})
            if file_res.status != 200:
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            file_data = await file_res.json()
            if not file_data.get("ok"):
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            file_path = file_data.get("result", {}).get("file_path")
            if not file_path:
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            # 3. Download actual binary image bytes
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            dl_res = await client.get(download_url)
            if dl_res.status != 200:
                AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + AVATAR_NEGATIVE_TTL}
                return None

            img_bytes = await dl_res.read()
            content_type = dl_res.headers.get("Content-Type", "image/jpeg")

            AVATAR_CACHE[cache_key] = {
                "data": img_bytes,
                "content_type": content_type,
                "expires_at": now + AVATAR_CACHE_TTL
            }
            return img_bytes, content_type
    except Exception as e:
        logger.debug("Failed to fetch Telegram avatar for @%s: %s", clean_username, e)
        AVATAR_CACHE[cache_key] = {"data": None, "content_type": "", "expires_at": now + 600}
        return None


def json_response_cors(data: dict, status: int = 200) -> web.Response:
    """Return JSON response with standard CORS headers."""
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Telegram-Init-Data",
        },
    )


async def handle_options(request: web.Request) -> web.Response:
    """Handle pre-flight CORS OPTIONS request."""
    return web.Response(
        status=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Telegram-Init-Data",
        },
    )


def validate_telegram_init_data(init_data_raw: str, bot_token: str) -> Optional[Dict[str, Any]]:
    """
    Validate Telegram WebApp initData string using HMAC-SHA256 according to Telegram specification:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data_raw or not bot_token:
        return None

    try:
        parsed = urllib.parse.parse_qs(init_data_raw, keep_blank_values=True)
        hash_val = parsed.get("hash", [None])[0]
        if not hash_val:
            return None

        # Build data-check-string from all pairs other than 'hash', sorted alphabetically
        pairs = []
        for k, v_list in parsed.items():
            if k == "hash":
                continue
            pairs.append(f"{k}={v_list[0]}")
        pairs.sort()
        data_check_string = "\n".join(pairs)

        # Secret key is HMAC-SHA256 of bot_token with key "WebAppData"
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, hash_val):
            return None

        # Validate auth_date freshness (up to 24 hours / 86400 seconds)
        auth_date_val = parsed.get("auth_date", [None])[0]
        if auth_date_val and auth_date_val.isdigit():
            auth_timestamp = int(auth_date_val)
            import time
            current_timestamp = int(time.time())
            # Reject if older than 24 hours (86400s) with 300s clock-drift margin
            if current_timestamp - auth_timestamp > 86400:
                logger.warning("Telegram initData expired (auth_date=%s)", auth_date_val)
                return None

        # Extract parsed user dict
        user_json_str = parsed.get("user", [None])[0]
        user_data = json.loads(user_json_str) if user_json_str else {}
        return {
            "user": user_data,
            "auth_date": auth_date_val,
            "query_id": parsed.get("query_id", [None])[0],
        }
    except Exception as e:
        logger.warning("Error validating Telegram initData: %s", e)
        return None


def get_authenticated_user(request: web.Request) -> Optional[Dict[str, Any]]:
    """
    Extract and validate Telegram user from request headers or query string.
    Returns user dict with at least 'id', 'username', 'first_name', etc., or None.
    """
    init_data_raw = ""

    # Check Authorization header: 'tma <initData>' or 'Bearer <initData>'
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("tma "):
        init_data_raw = auth_header[4:].strip()
    elif auth_header.startswith("Bearer "):
        init_data_raw = auth_header[7:].strip()

    # Check custom header
    if not init_data_raw:
        init_data_raw = request.headers.get("X-Telegram-Init-Data", "").strip()

    # Check query param
    if not init_data_raw:
        init_data_raw = request.query.get("initData", "").strip()

    if init_data_raw:
        validated = validate_telegram_init_data(init_data_raw, settings.BOT_TOKEN)
        if validated and "user" in validated and validated["user"].get("id"):
            return validated["user"]

    # Fallback for explicit development/test mode only
    if settings.ENVIRONMENT.lower() in ("dev", "development", "local", "test"):
        dev_uid = request.headers.get("X-Dev-User-Id")
        if dev_uid and dev_uid.isdigit():
            return {
                "id": int(dev_uid),
                "username": "dev_user",
                "first_name": "Dev User",
            }

    return None


def require_authenticated_user(request: web.Request) -> Dict[str, Any]:
    """Ensure request originates from a validated Telegram user."""
    user = get_authenticated_user(request)
    if not user:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": "Unauthorized: invalid or missing Telegram initData"}),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return user


def require_admin(request: web.Request) -> Dict[str, Any]:
    """Ensure request originates from an authorized Administrator."""
    user = require_authenticated_user(request)
    user_id = user.get("id")
    if not user_id or not settings.is_admin(user_id):
        raise web.HTTPForbidden(
            text=json.dumps({"ok": False, "error": "Forbidden: administrative privileges required"}),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return user


# ==============================================================================
# Public Endpoints
# ==============================================================================

async def handle_index(request: web.Request) -> web.FileResponse:
    """Serve the Mini App main index.html file."""
    index_file = WEBAPP_DIR / "index.html"
    return web.FileResponse(index_file)


async def handle_get_categories(request: web.Request) -> web.Response:
    """
    GET /api/categories
    Returns all active categories with counts of active bots in each.
    """
    try:
        async with async_session_factory() as session:
            stmt = (
                select(
                    Category,
                    func.count(Bot.id).filter(Bot.is_active.is_(True)).label("bot_count"),
                )
                .outerjoin(Bot, Category.id == Bot.category_id)
                .where(Category.is_active.is_(True))
                .group_by(Category.id)
                .order_by(Category.sort_order.asc(), Category.id.asc())
            )
            result = await session.execute(stmt)
            rows = result.all()

            categories_list = [
                {
                    "id": cat.id,
                    "name": cat.name,
                    "emoji": cat.emoji,
                    "description": cat.description or "",
                    "sort_order": cat.sort_order,
                    "bot_count": count or 0,
                }
                for cat, count in rows
            ]

            return json_response_cors({"ok": True, "categories": categories_list})
    except Exception as e:
        logger.error("Error fetching categories for Mini App: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Internal server error"}, status=500)


async def handle_get_bots(request: web.Request) -> web.Response:
    """
    GET /api/bots?category_id=...&query=...
    Returns active bots, optionally filtered by category or search term.
    Includes is_favorite: bool for authenticated user.
    """
    category_id_param = request.query.get("category_id")
    query_param = request.query.get("query", "").strip()

    try:
        # Check if user is authenticated to populate is_favorite flags
        auth_user = get_authenticated_user(request)
        user_id = int(auth_user["id"]) if auth_user and auth_user.get("id") else None

        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            fav_repo = FavoriteRepository(session)

            if query_param:
                bots = await bot_repo.search(query=query_param, active_only=True, limit=50)
            elif category_id_param and category_id_param.isdigit():
                bots = await bot_repo.get_by_category(category_id=int(category_id_param), active_only=True)
            else:
                stmt = (
                    select(Bot)
                    .join(Bot.category)
                    .where(Bot.is_active.is_(True), Category.is_active.is_(True))
                    .order_by(Bot.sort_order.asc(), Bot.id.asc())
                )
                result = await session.execute(stmt)
                bots = list(result.scalars().all())

            favorite_bot_ids = set()
            if user_id:
                favorite_bot_ids = await fav_repo.get_user_favorite_bot_ids(user_id)

            bots_list = [
                {
                    "id": b.id,
                    "category_id": b.category_id,
                    "name": b.name,
                    "username": b.username,
                    "emoji": b.emoji,
                    "description": b.description or "",
                    "sort_order": b.sort_order,
                    "is_favorite": (b.id in favorite_bot_ids),
                }
                for b in bots
            ]

            return json_response_cors({"ok": True, "bots": bots_list})
    except Exception as e:
        logger.error("Error fetching bots for Mini App: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Internal server error"}, status=500)


async def handle_get_favorites(request: web.Request) -> web.Response:
    """
    GET /api/favorites
    Returns list of favorite bots for current authenticated Telegram user.
    """
    auth_user = require_authenticated_user(request)
    user_id = int(auth_user["id"])

    try:
        async with async_session_factory() as session:
            fav_repo = FavoriteRepository(session)
            favorite_bots = await fav_repo.get_user_favorites(user_id)

            favorites_list = [
                {
                    "id": b.id,
                    "category_id": b.category_id,
                    "name": b.name,
                    "username": b.username,
                    "emoji": b.emoji,
                    "description": b.description or "",
                    "sort_order": b.sort_order,
                    "is_favorite": True,
                }
                for b in favorite_bots
            ]

            return json_response_cors({"ok": True, "favorites": favorites_list})
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching favorites for user %d: %s", user_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Internal server error"}, status=500)


async def handle_post_favorite(request: web.Request) -> web.Response:
    """
    POST /api/favorites/{id}
    Adds a bot to the authenticated user's favorites list.
    """
    auth_user = require_authenticated_user(request)
    user_id = int(auth_user["id"])

    bot_id_str = request.match_info.get("id")
    if not bot_id_str or not bot_id_str.isdigit():
        return json_response_cors({"ok": False, "error": "Invalid bot_id"}, status=400)
    bot_id = int(bot_id_str)

    try:
        async with async_session_factory() as session:
            fav_repo = FavoriteRepository(session)
            success = await fav_repo.add_favorite(user_id=user_id, bot_id=bot_id)
            if not success:
                return json_response_cors({"ok": False, "error": "Бот не найден или неактивен"}, status=404)

            return json_response_cors({"ok": True, "is_favorite": True})
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error("Error adding favorite for user %d, bot %d: %s", user_id, bot_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Internal server error"}, status=500)


async def handle_delete_favorite(request: web.Request) -> web.Response:
    """
    DELETE /api/favorites/{id}
    Removes a bot from the authenticated user's favorites list.
    """
    auth_user = require_authenticated_user(request)
    user_id = int(auth_user["id"])

    bot_id_str = request.match_info.get("id")
    if not bot_id_str or not bot_id_str.isdigit():
        return json_response_cors({"ok": False, "error": "Invalid bot_id"}, status=400)
    bot_id = int(bot_id_str)

    try:
        async with async_session_factory() as session:
            fav_repo = FavoriteRepository(session)
            await fav_repo.remove_favorite(user_id=user_id, bot_id=bot_id)
            return json_response_cors({"ok": True, "is_favorite": False})
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting favorite for user %d, bot %d: %s", user_id, bot_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Internal server error"}, status=500)



async def handle_get_bot_avatar(request: web.Request) -> web.Response:
    """
    GET /api/bots/{id}/avatar
    Returns bot avatar image (original from Telegram with caching, or SVG fallback).
    """
    bot_id_str = request.match_info.get("id")
    if not bot_id_str or not bot_id_str.isdigit():
        svg_data = generate_fallback_svg("🤖", "Bot")
        return web.Response(
            body=svg_data,
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"}
        )

    bot_id = int(bot_id_str)
    try:
        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            bot = await bot_repo.get_by_id(bot_id)
            if not bot:
                svg_data = generate_fallback_svg("🤖", "Bot")
                return web.Response(
                    body=svg_data,
                    content_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"}
                )

            avatar_res = await fetch_telegram_avatar(bot.username, settings.BOT_TOKEN)
            if avatar_res:
                img_data, c_type = avatar_res
                return web.Response(
                    body=img_data,
                    content_type=c_type,
                    headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"}
                )

            # Clean SVG Fallback with bot's emoji
            svg_data = generate_fallback_svg(bot.emoji, bot.name)
            return web.Response(
                body=svg_data,
                content_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"}
            )
    except Exception as e:
        logger.warning("Error in handle_get_bot_avatar: %s", e)
        svg_data = generate_fallback_svg("🤖", "Bot")
        return web.Response(
            body=svg_data,
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"}
        )


async def handle_post_click(request: web.Request) -> web.Response:
    """
    POST /api/click
    Logs bot click redirect for analytics.
    Payload: {"bot_id": 1}
    Uses authenticated user ID from initData if present.
    """
    try:
        data = await request.json()
        bot_id = data.get("bot_id")
        if not bot_id or not isinstance(bot_id, int):
            return json_response_cors({"ok": False, "error": "Invalid bot_id"}, status=400)

        # Authenticated user has priority, otherwise fallback to request payload
        auth_user = get_authenticated_user(request)
        user_id = auth_user["id"] if auth_user else (data.get("user_id") or 0)

        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            await bot_repo.log_click(user_id=int(user_id), bot_id=bot_id)

        return json_response_cors({"ok": True})
    except Exception as e:
        logger.warning("Error logging bot click from Mini App: %s", e)
        return json_response_cors({"ok": False, "error": "Failed to log click"}, status=400)


# ==============================================================================
# User Profile Endpoint
# ==============================================================================

async def handle_get_me(request: web.Request) -> web.Response:
    """
    GET /api/me
    Returns current authenticated user profile & personal analytics.
    """
    auth_user = require_authenticated_user(request)
    tg_id = int(auth_user["id"])

    try:
        async with async_session_factory() as session:
            user_repo = UserRepository(session)
            bot_repo = BotRepository(session)

            user, _ = await user_repo.get_or_create(
                telegram_id=tg_id,
                username=auth_user.get("username"),
                first_name=auth_user.get("first_name"),
            )
            clicks_count = await bot_repo.get_user_clicks_count(tg_id)
            is_adm = settings.is_admin(tg_id)

            return json_response_cors({
                "ok": True,
                "user": {
                    "telegram_id": user.telegram_id,
                    "username": user.username or "",
                    "first_name": user.first_name or "",
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "last_activity": user.last_activity.isoformat() if user.last_activity else None,
                    "clicks_count": clicks_count,
                    "is_admin": is_adm,
                },
            })
    except Exception as e:
        logger.error("Error fetching user profile in /api/me: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to load user profile"}, status=500)


# ==============================================================================
# Admin Endpoints (Strictly Protected)
# ==============================================================================

async def handle_admin_get_stats(request: web.Request) -> web.Response:
    """GET /api/admin/stats"""
    require_admin(request)

    try:
        async with async_session_factory() as session:
            user_repo = UserRepository(session)
            cat_repo = CategoryRepository(session)
            bot_repo = BotRepository(session)

            total_users = await user_repo.get_total_count()
            active_cats = await cat_repo.get_count_active()
            total_cats = await cat_repo.get_count_all()
            active_bots = await bot_repo.get_count_active()
            total_clicks = await bot_repo.get_total_clicks()
            popular_raw = await bot_repo.get_popular_bots(limit=5)

            popular_bots = [
                {
                    "id": b.id,
                    "name": b.name,
                    "username": b.username,
                    "emoji": b.emoji,
                    "clicks": count,
                }
                for b, count in popular_raw
            ]

            return json_response_cors({
                "ok": True,
                "stats": {
                    "total_users": total_users,
                    "active_categories": active_cats,
                    "total_categories": total_cats,
                    "active_bots": active_bots,
                    "total_clicks": total_clicks,
                    "popular_bots": popular_bots,
                },
            })
    except Exception as e:
        logger.error("Error fetching admin stats: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to fetch stats"}, status=500)


async def handle_admin_get_categories(request: web.Request) -> web.Response:
    """GET /api/admin/categories - all categories with bot counts."""
    require_admin(request)

    try:
        async with async_session_factory() as session:
            stmt = (
                select(Category, func.count(Bot.id).label("bot_count"))
                .outerjoin(Bot, Category.id == Bot.category_id)
                .group_by(Category.id)
                .order_by(Category.sort_order.asc(), Category.id.asc())
            )
            result = await session.execute(stmt)
            rows = result.all()

            categories_list = [
                {
                    "id": cat.id,
                    "name": cat.name,
                    "emoji": cat.emoji,
                    "description": cat.description or "",
                    "sort_order": cat.sort_order,
                    "is_active": cat.is_active,
                    "bot_count": count or 0,
                }
                for cat, count in rows
            ]

            return json_response_cors({"ok": True, "categories": categories_list})
    except Exception as e:
        logger.error("Error fetching admin categories: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to fetch categories"}, status=500)


async def handle_admin_create_category(request: web.Request) -> web.Response:
    """POST /api/admin/categories"""
    require_admin(request)

    try:
        data = await request.json()
        name = (data.get("name") or "").strip()
        emoji = (data.get("emoji") or "📁").strip()
        description = (data.get("description") or "").strip()

        if not name:
            return json_response_cors({"ok": False, "error": "Название категории не может быть пустым"}, status=400)

        async with async_session_factory() as session:
            cat_repo = CategoryRepository(session)
            cat = await cat_repo.create(name=name, emoji=emoji, description=description)
            return json_response_cors({
                "ok": True,
                "category": {
                    "id": cat.id,
                    "name": cat.name,
                    "emoji": cat.emoji,
                    "description": cat.description,
                    "sort_order": cat.sort_order,
                    "is_active": cat.is_active,
                },
            }, status=201)
    except Exception as e:
        logger.error("Error creating category: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to create category"}, status=500)


async def handle_admin_update_category(request: web.Request) -> web.Response:
    """PUT /api/admin/categories/{id}"""
    require_admin(request)
    cat_id = int(request.match_info["id"])

    try:
        data = await request.json()
        async with async_session_factory() as session:
            cat_repo = CategoryRepository(session)
            cat = await cat_repo.update(cat_id, **data)
            if not cat:
                return json_response_cors({"ok": False, "error": "Категория не найдена"}, status=404)

            return json_response_cors({
                "ok": True,
                "category": {
                    "id": cat.id,
                    "name": cat.name,
                    "emoji": cat.emoji,
                    "description": cat.description,
                    "sort_order": cat.sort_order,
                    "is_active": cat.is_active,
                },
            })
    except Exception as e:
        logger.error("Error updating category ID=%d: %s", cat_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to update category"}, status=500)


async def handle_admin_toggle_category(request: web.Request) -> web.Response:
    """POST /api/admin/categories/{id}/toggle"""
    require_admin(request)
    cat_id = int(request.match_info["id"])

    try:
        async with async_session_factory() as session:
            cat_repo = CategoryRepository(session)
            new_state = await cat_repo.toggle_active(cat_id)
            if new_state is None:
                return json_response_cors({"ok": False, "error": "Категория не найдена"}, status=404)
            return json_response_cors({"ok": True, "is_active": new_state})
    except Exception as e:
        logger.error("Error toggling category ID=%d: %s", cat_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to toggle category"}, status=500)


async def handle_admin_order_category(request: web.Request) -> web.Response:
    """POST /api/admin/categories/{id}/order {"direction": "up"|"down"}"""
    require_admin(request)
    cat_id = int(request.match_info["id"])

    try:
        data = await request.json()
        direction = data.get("direction", "up")
        async with async_session_factory() as session:
            cat_repo = CategoryRepository(session)
            success = await cat_repo.change_order(cat_id, direction)
            return json_response_cors({"ok": success})
    except Exception as e:
        logger.error("Error reordering category ID=%d: %s", cat_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to reorder category"}, status=500)


async def handle_admin_delete_category(request: web.Request) -> web.Response:
    """DELETE /api/admin/categories/{id}"""
    require_admin(request)
    cat_id = int(request.match_info["id"])

    try:
        async with async_session_factory() as session:
            cat_repo = CategoryRepository(session)
            deleted = await cat_repo.delete(cat_id)
            if not deleted:
                return json_response_cors({"ok": False, "error": "Категория не найдена"}, status=404)
            return json_response_cors({"ok": True})
    except Exception as e:
        logger.error("Error deleting category ID=%d: %s", cat_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to delete category"}, status=500)


async def handle_admin_get_bots(request: web.Request) -> web.Response:
    """GET /api/admin/bots?category_id=..."""
    require_admin(request)
    cat_id_param = request.query.get("category_id")
    category_id = int(cat_id_param) if cat_id_param and cat_id_param.isdigit() else None

    try:
        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            bots = await bot_repo.get_all_admin(category_id=category_id)

            bots_list = [
                {
                    "id": b.id,
                    "category_id": b.category_id,
                    "category_name": b.category.name if b.category else "",
                    "category_emoji": b.category.emoji if b.category else "📁",
                    "name": b.name,
                    "username": b.username,
                    "emoji": b.emoji,
                    "description": b.description or "",
                    "sort_order": b.sort_order,
                    "is_active": b.is_active,
                }
                for b in bots
            ]

            return json_response_cors({"ok": True, "bots": bots_list})
    except Exception as e:
        logger.error("Error fetching admin bots: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to fetch bots"}, status=500)


async def handle_admin_create_bot(request: web.Request) -> web.Response:
    """POST /api/admin/bots"""
    require_admin(request)

    try:
        data = await request.json()
        category_id = data.get("category_id")
        name = (data.get("name") or "").strip()
        username = (data.get("username") or "").strip()
        description = (data.get("description") or "").strip()
        emoji = (data.get("emoji") or "🤖").strip()

        if not category_id or not name or not username:
            return json_response_cors(
                {"ok": False, "error": "Укажите категорию, название и username бота"}, status=400
            )

        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            bot = await bot_repo.create(
                category_id=int(category_id),
                name=name,
                username=username,
                description=description,
                emoji=emoji,
            )
            return json_response_cors({
                "ok": True,
                "bot": {
                    "id": bot.id,
                    "category_id": bot.category_id,
                    "name": bot.name,
                    "username": bot.username,
                    "emoji": bot.emoji,
                    "description": bot.description,
                    "sort_order": bot.sort_order,
                    "is_active": bot.is_active,
                },
            }, status=201)
    except Exception as e:
        logger.error("Error creating bot: %s", e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to create bot"}, status=500)


async def handle_admin_update_bot(request: web.Request) -> web.Response:
    """PUT /api/admin/bots/{id}"""
    require_admin(request)
    bot_id = int(request.match_info["id"])

    try:
        data = await request.json()
        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            bot = await bot_repo.update(bot_id, **data)
            if not bot:
                return json_response_cors({"ok": False, "error": "Бот не найден"}, status=404)

            return json_response_cors({
                "ok": True,
                "bot": {
                    "id": bot.id,
                    "category_id": bot.category_id,
                    "name": bot.name,
                    "username": bot.username,
                    "emoji": bot.emoji,
                    "description": bot.description,
                    "sort_order": bot.sort_order,
                    "is_active": bot.is_active,
                },
            })
    except Exception as e:
        logger.error("Error updating bot ID=%d: %s", bot_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to update bot"}, status=500)


async def handle_admin_toggle_bot(request: web.Request) -> web.Response:
    """POST /api/admin/bots/{id}/toggle"""
    require_admin(request)
    bot_id = int(request.match_info["id"])

    try:
        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            new_state = await bot_repo.toggle_active(bot_id)
            if new_state is None:
                return json_response_cors({"ok": False, "error": "Бот не найден"}, status=404)
            return json_response_cors({"ok": True, "is_active": new_state})
    except Exception as e:
        logger.error("Error toggling bot ID=%d: %s", bot_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to toggle bot"}, status=500)


async def handle_admin_order_bot(request: web.Request) -> web.Response:
    """POST /api/admin/bots/{id}/order {"direction": "up"|"down"}"""
    require_admin(request)
    bot_id = int(request.match_info["id"])

    try:
        data = await request.json()
        direction = data.get("direction", "up")
        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            success = await bot_repo.change_order(bot_id, direction)
            return json_response_cors({"ok": success})
    except Exception as e:
        logger.error("Error reordering bot ID=%d: %s", bot_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to reorder bot"}, status=500)


async def handle_admin_delete_bot(request: web.Request) -> web.Response:
    """DELETE /api/admin/bots/{id}"""
    require_admin(request)
    bot_id = int(request.match_info["id"])

    try:
        async with async_session_factory() as session:
            bot_repo = BotRepository(session)
            deleted = await bot_repo.delete(bot_id)
            if not deleted:
                return json_response_cors({"ok": False, "error": "Бот не найден"}, status=404)
            return json_response_cors({"ok": True})
    except Exception as e:
        logger.error("Error deleting bot ID=%d: %s", bot_id, e, exc_info=True)
        return json_response_cors({"ok": False, "error": "Failed to delete bot"}, status=500)


# ==============================================================================
# Application Factory
# ==============================================================================

def create_webapp_app() -> web.Application:
    """Build and configure the aiohttp Web Application."""
    app = web.Application()

    # CORS Pre-flight handler
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)

    # Public Endpoints
    app.router.add_get("/api/categories", handle_get_categories)
    app.router.add_get("/api/bots", handle_get_bots)
    app.router.add_get("/api/bots/{id:\\d+}/avatar", handle_get_bot_avatar)
    app.router.add_post("/api/click", handle_post_click)

    # Favorites Endpoints (Telegram initData authenticated)
    app.router.add_get("/api/favorites", handle_get_favorites)
    app.router.add_post("/api/favorites/{id:\\d+}", handle_post_favorite)
    app.router.add_delete("/api/favorites/{id:\\d+}", handle_delete_favorite)

    # User Profile Endpoint
    app.router.add_get("/api/me", handle_get_me)

    # Admin Endpoints
    app.router.add_get("/api/admin/stats", handle_admin_get_stats)
    app.router.add_get("/api/admin/categories", handle_admin_get_categories)
    app.router.add_post("/api/admin/categories", handle_admin_create_category)
    app.router.add_put("/api/admin/categories/{id:\\d+}", handle_admin_update_category)
    app.router.add_post("/api/admin/categories/{id:\\d+}/toggle", handle_admin_toggle_category)
    app.router.add_post("/api/admin/categories/{id:\\d+}/order", handle_admin_order_category)
    app.router.add_delete("/api/admin/categories/{id:\\d+}", handle_admin_delete_category)

    app.router.add_get("/api/admin/bots", handle_admin_get_bots)
    app.router.add_post("/api/admin/bots", handle_admin_create_bot)
    app.router.add_put("/api/admin/bots/{id:\\d+}", handle_admin_update_bot)
    app.router.add_post("/api/admin/bots/{id:\\d+}/toggle", handle_admin_toggle_bot)
    app.router.add_post("/api/admin/bots/{id:\\d+}/order", handle_admin_order_bot)
    app.router.add_delete("/api/admin/bots/{id:\\d+}", handle_admin_delete_bot)

    # Main Web App UI & Static files
    app.router.add_get("/", handle_index)
    app.router.add_static("/static", path=WEBAPP_DIR, name="static")

    return app


async def start_webapp_runner(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start the aiohttp web server runner asynchronously."""
    app = create_webapp_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Telegram Mini App server running at http://%s:%d (URL: %s)", host, port, settings.WEBAPP_URL)
    return runner
