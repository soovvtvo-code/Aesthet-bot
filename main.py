import asyncio
import json
import logging
import os
import re
import requests
from collections import defaultdict
from contextlib import asynccontextmanager
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from urllib.parse import quote
from duckduckgo_search import DDGS

import uvicorn
from fastapi import FastAPI, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, MenuButtonWebApp, Update,
)
from aiogram.filters import Command

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "8748736196:AAHBGM7DnJaYZKS43h5XOT_Lc9PLXc8sOic")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DEV_DOMAIN     = os.environ.get("REPLIT_DEV_DOMAIN", "localhost")
MINI_APP_URL   = os.environ.get("MINI_APP_URL", "")
APP_URL        = MINI_APP_URL if MINI_APP_URL else f"https://{DEV_DOMAIN}"
IS_PRODUCTION  = bool(MINI_APP_URL)
PORT           = int(os.environ.get("PORT", 8000))
WEBHOOK_PATH   = "/webhook"

HISTORY_LIMIT = 5
HISTORY_FILE  = os.path.join(os.path.dirname(__file__), "history.json")
ALBUM_TIMEOUT = 0.5

PINTEREST_RE = re.compile(
    r"https?://(www\.)?(pinterest\.(com|ru|co\.uk|fr|de|es|it|jp|pt|nz|ca|au)|pin\.it)/\S+"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── State ─────────────────────────────────────────────────────────────────────

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

album_buffer: dict[str, list[Message]] = {}
album_tasks:  dict[str, asyncio.Task]  = {}


# ── History ───────────────────────────────────────────────────────────────────

def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return defaultdict(list)
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return defaultdict(list, {int(k): v for k, v in raw.items()})
    except Exception as e:
        logging.warning(f"History load error: {e}")
        return defaultdict(list)

def persist_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(user_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"History save error: {e}")

user_history: dict = load_history()

def save_to_history(user_id: int, queries: dict):
    h = user_history[user_id]
    wb = queries.get("wb", "")
    h = [item for item in h if (item.get("wb", "") if isinstance(item, dict) else item) != wb]
    h.insert(0, queries)
    user_history[user_id] = h[:HISTORY_LIMIT]
    persist_history()


# ── Umniy Poisk (Gemini Flash + Агрессивный промпт + DuckDuckGo) ──────────────

def analyze_image(image_bytes: bytes) -> dict:
    # ШАГ 1: Агрессивный поиск бренда (режим "Эксперт-оценщик")
    prompt_1 = (
        "Ты — элитный fashion-оценщик и историк моды. Изучи фото. "
        "Твоя главная цель — узнать ТОЧНЫЙ бренд и модель вещи (например, часы Jaeger-LeCoultre Étrier, сумка Jacquemus Le Chiquito). "
        "Если бренд распознан, обязательно назови его. Если вещь обычная — опиши её сухо (тип, материал, цвет)."
    )
    
    # ИСПОЛЬЗУЕМ БЕЗОТКАЗНУЮ МОДЕЛЬ FLASH
    resp_1 = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            prompt_1,
        ],
    )
    base_query = resp_1.text.strip()

    # ШАГ 2: Сверка с рынком для поиска аналогов
    ddg_results = []
    try:
        with DDGS() as ddgs:
            # Ищем аналоги без слова "купить", чтобы не собирать дешевый спам
            results = ddgs.text(f"{base_query} стиль", region='ru-ru', max_results=4)
            ddg_results = [r.get('title', '') for r in results]
    except Exception as e:
        logging.warning(f"DDG Search error: {e}")

    # ШАГ 3: Разделение логики (Точное имя отдельно, маркетплейсы отдельно)
    market_context = "\n".join(ddg_results) if ddg_results else "Нет данных из сети."
    
    prompt_2 = f"""Вот первоначальный анализ вещи от эксперта: {base_query}
    Вот заголовки из сети: {market_context}

    Твоя задача:
    1. Для поля 'item_name' возьми самое точное и красивое название из анализа эксперта. Если там упомянут бренд (например, Jaeger-LeCoultre) — он ОБЯЗАТЕЛЬНО должен быть в item_name.
    2. Для маркетплейсов (wb, oz, ali, ym) создай упрощенные запросы. Маркетплейсы не умеют искать люкс. Опиши вещь так, чтобы найти ПОХОЖУЮ.

    Верни ТОЛЬКО JSON, без markdown, без пояснений, строго в таком формате:
    {{
      "item_name": "Точное название вещи с брендом",
      "wb": "запрос для Wildberries (3-4 слова, без люксовых брендов)",
      "oz": "запрос для Ozon (чуть точнее)",
      "ali": "query for AliExpress (in English)",
      "ym": "запрос для Яндекс Маркет"
    }}"""

    # ИСПОЛЬЗУЕМ БЕЗОТКАЗНУЮ МОДЕЛЬ FLASH
    resp_2 = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            prompt_2,
        ],
    )

    text = resp_2.text.strip()
    text = re.sub(r"```json\s*|\s*```", "", text).strip()
    queries = json.loads(text)
    
    fallback = queries.get("wb") or queries.get("oz") or "Товар"
    queries.setdefault("item_name", fallback)
    queries.setdefault("wb", fallback)
    queries.setdefault("oz", fallback)
    queries.setdefault("ali", fallback)
    queries.setdefault("ym", fallback)
    
    return queries


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_keyboard(queries: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Wildberries",
                url=f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote(queries['wb'])}"),
            InlineKeyboardButton(text="Ozon",
                url=f"https://www.ozon.ru/search/?text={quote(queries['oz'])}"),
        ],
        [
            InlineKeyboardButton(text="AliExpress",
                url=f"https://aliexpress.ru/wholesale?SearchText={quote(queries['ali'])}"),
            InlineKeyboardButton(text="Яндекс Маркет",
                url=f"https://market.yandex.ru/search?text={quote(queries['ym'])}"),
        ],
    ])

def fetch_pinterest_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
        if not og:
            return None
        img = requests.get(og["content"], headers=HEADERS, timeout=10)
        img.raise_for_status()
        return img.content
    except Exception as e:
        logging.warning(f"Pinterest fetch error: {e}")
        return None


# ── Bot handlers ──────────────────────────────────────────────────────────────

async def process_single_photo(message: Message, image_data: bytes):
    try:
        queries = analyze_image(image_data)
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        await message.answer("Не удалось распознать товар.")
        return
    save_to_history(message.from_user.id, queries)
    
    display = queries.get("item_name", queries.get("wb", ""))
    await message.answer(f"*{display}*", parse_mode="Markdown",
                         reply_markup=build_keyboard(queries))

async def download_photo(message: Message) -> bytes | None:
    try:
        photo = message.photo[-1]
        file  = await bot.get_file(photo.file_id)
        fb    = await bot.download_file(file.file_path)
        return fb.read()
    except Exception as e:
        logging.warning(f"Photo download error: {e}")
        return None

async def process_album(group_id: str):
    await asyncio.sleep(ALBUM_TIMEOUT)
    messages = album_buffer.pop(group_id, [])
    album_tasks.pop(group_id, None)
    if not messages:
        return
    images = await asyncio.gather(*[download_photo(m) for m in messages])
    await asyncio.gather(*[
        process_single_photo(msg, data)
        for msg, data in zip(messages, images) if data
    ])


@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔍 Открыть Aesthet", web_app=WebAppInfo(url=APP_URL))
    ]])
    welcome = (
        "👋 Привет! Я *Aesthet* — бот для поиска товаров по фото.\n\n"
        "📸 Просто отправь мне фото любой вещи, и я найду её на:\n"
        "• Wildberries\n"
        "• Ozon\n"
        "• AliExpress\n"
        "• Яндекс Маркет\n\n"
        "Или открой мини-приложение ниже и загрузи фото там 👇"
    )
    await message.answer(welcome, parse_mode="Markdown", reply_markup=keyboard)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    history = user_history.get(message.from_user.id, [])
    if not history:
        await message.answer("История пуста.")
        return
    buttons = []
    for i, item in enumerate(history):
        label = item.get("item_name", item.get("wb", "")) if isinstance(item, dict) else item
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"hsearch:{i}")])
    await message.answer("История",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("hsearch:"))
async def handle_history_search(callback):
    idx = int(callback.data[len("hsearch:"):])
    history = user_history.get(callback.from_user.id, [])
    if idx >= len(history):
        await callback.answer("Запись не найдена")
        return
    item = history[idx]
    if isinstance(item, dict):
        queries = item
    else:
        queries = {"item_name": item, "wb": item, "oz": item, "ali": item, "ym": item}
    await callback.answer()
    display = queries.get("item_name", queries.get("wb", ""))
    await callback.message.answer(f"*{display}*", parse_mode="Markdown",
                                   reply_markup=build_keyboard(queries))


@dp.message(F.photo)
async def handle_photo(message: Message):
    group_id = message.media_group_id
    if group_id:
        album_buffer.setdefault(group_id, []).append(message)
        if group_id in album_tasks:
            album_tasks[group_id].cancel()
        album_tasks[group_id] = asyncio.create_task(process_album(group_id))
    else:
        data = await download_photo(message)
        if data:
            await process_single_photo(message, data)


@dp.message(F.text)
async def handle_text(message: Message):
    text  = message.text or ""
    match = PINTEREST_RE.search(text)
    if not match:
        await message.answer("Отправьте фото или ссылку из Pinterest.")
        return
    data = fetch_pinterest_image(match.group(0))
    if not data:
        await message.answer("Не удалось загрузить изображение. Отправьте фото напрямую.")
        return
    await process_single_photo(message, data)


# ── FastAPI lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=APP_URL))
        )
    except Exception:
        pass

    if IS_PRODUCTION:
        webhook_url = f"{APP_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logging.info(f"Webhook set: {webhook_url}")
        yield
        await bot.delete_webhook()
    else:
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        yield
        task.cancel()

    await bot.session.close()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<html><body><h1>Aesthet Bot is running</h1></body></html>")
    with open(index_path, encoding="utf-8") as f:
        return f.read()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response()


@app.post("/analyze")
async def analyze_endpoint(file: UploadFile = File(...)):
    image_bytes = await file.read()
    try:
        queries = analyze_image(image_bytes)
    except Exception as e:
        logging.error(f"Analyze endpoint error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return {
        "item_name": queries.get("item_name", "Неизвестный товар"),
        "wb":  f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote(queries['wb'])}",
        "oz":  f"https://www.ozon.ru/search/?text={quote(queries['oz'])}",
        "ali": f"https://aliexpress.ru/wholesale?SearchText={quote(queries['ali'])}",
        "ym":  f"https://market.yandex.ru/search?text={quote(queries['ym'])}",
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, app_dir=os.path.dirname(__file__))
