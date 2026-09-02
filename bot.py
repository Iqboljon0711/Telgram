import asyncio
import logging
import os
import time
import zlib

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from google import genai

# ---------------------------------------------------------------------------
# Sozlamalar — SECRETS ARE NEVER HARDCODED. Put them in a local .env file
# (see .env.example) which must NOT be committed to git or pasted anywhere.
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
TOPSMM_API_KEY = os.environ["TOPSMM_API_KEY"]
TOPSMM_URL = os.environ.get("TOPSMM_URL", "https://topsmm.uz/api/v2")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MARKUP_MULTIPLIER = float(os.environ.get("MARKUP_MULTIPLIER", "1.25"))
# gemini-2.5-flash was retired by Google ahead of schedule (404 NOT_FOUND).
# Current model as of Sep 2026: gemini-3.7-flash. Configurable via .env so a
# future Google renaming doesn't require editing this file again.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
SERVICES_CACHE_TTL = 300  # seconds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# In-memory caches (fine for a single-process bot; use Redis if you scale out)
_services_cache: dict = {"data": [], "fetched_at": 0}
_category_lookup: dict[str, str] = {}   # short id -> full category name


class OrderState(StatesGroup):
    waiting_for_search = State()
    waiting_for_link = State()
    waiting_for_quantity = State()
    waiting_for_confirm = State()


def short_id(text: str) -> str:
    """Stable short id for callback_data (Telegram limits callback_data to 64 bytes)."""
    return format(zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF, "x")


async def fetch_services(session: aiohttp.ClientSession, force: bool = False) -> list:
    """Fetch services from TopSMM with a markup applied, cached for SERVICES_CACHE_TTL seconds."""
    now = time.time()
    if not force and _services_cache["data"] and now - _services_cache["fetched_at"] < SERVICES_CACHE_TTL:
        return _services_cache["data"]

    data = {"key": TOPSMM_API_KEY, "action": "services"}
    try:
        async with session.post(TOPSMM_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            services = await resp.json(content_type=None)
    except Exception:
        logger.exception("Xizmatlarni olishda xatolik")
        return _services_cache["data"]  # fall back to stale cache rather than nothing

    if not isinstance(services, list):
        logger.error("Kutilmagan javob formati: %r", services)
        return _services_cache["data"]

    for service in services:
        try:
            base_rate = float(service.get("rate", 0))
        except (TypeError, ValueError):
            base_rate = 0.0
        service["rate"] = round(base_rate * MARKUP_MULTIPLIER, 2)

    _services_cache["data"] = services
    _services_cache["fetched_at"] = now
    return services


async def create_topsmm_order(session: aiohttp.ClientSession, service_id: int, link: str, quantity: int) -> dict | None:
    data = {
        "key": TOPSMM_API_KEY,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }
    try:
        async with session.post(TOPSMM_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json(content_type=None)
    except Exception:
        logger.exception("Buyurtma xatosi")
        return None


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Kategoriyalar bo'yicha", callback_data="show_categories")],
            [InlineKeyboardButton(text="🔍 AI yordamida qidirish", callback_data="ai_search_prompt")],
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Salom! SMM xizmatlari botiga xush kelibsiz.\n"
        "Qanday qilib xizmat topmoqchisiz?",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Bekor qilinadigan hech narsa yo'q.")
        return
    await state.clear()
    await message.answer("❎ Bekor qilindi.", reply_markup=main_menu_keyboard())


# --- KATEGORIYALAR ---
@dp.callback_query(F.data == "show_categories")
async def show_categories(callback: types.CallbackQuery, state: FSMContext):
    async with aiohttp.ClientSession() as session:
        services = await fetch_services(session)

    if not services:
        await callback.message.answer("Hozirda xizmatlar topilmadi. Birozdan so'ng qayta urinib ko'ring.")
        await callback.answer()
        return

    categories = sorted({s.get("category", "Boshqa") for s in services})
    keyboard_buttons = []
    for cat in categories:
        cid = short_id(cat)
        _category_lookup[cid] = cat  # exact lookup, no truncation collisions
        keyboard_buttons.append([InlineKeyboardButton(text=cat[:40], callback_data=f"cat_{cid}")])

    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="back_home")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Ijtimoiy tarmoq yoki bo'limni tanlang:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "back_home")
async def back_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Asosiy menyu:", reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("cat_"))
async def show_services_in_category(callback: types.CallbackQuery):
    cid = callback.data.removeprefix("cat_")
    selected_cat = _category_lookup.get(cid)
    if selected_cat is None:
        await callback.answer("Kategoriya eskirgan, qaytadan tanlang.", show_alert=True)
        return

    async with aiohttp.ClientSession() as session:
        services = await fetch_services(session)

    keyboard_buttons = []
    for s in services:
        if s.get("category") == selected_cat:
            s_id = s.get("service")
            s_name = s.get("name", "Nomsiz xizmat")
            s_rate = s.get("rate")
            btn_text = f"{s_name[:25]} (1000 ta) — {s_rate} so'm"
            keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"srv_{s_id}")])

    if not keyboard_buttons:
        await callback.message.answer("Bu bo'limda xizmatlar topilmadi.")
        await callback.answer()
        return

    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_categories")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Xizmatni va uning 1000 ta narxini ko'rib tanlang:", reply_markup=keyboard)
    await callback.answer()


# --- AI ORQALI QIDIRISH ---
@dp.callback_query(F.data == "ai_search_prompt")
async def ai_search_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🤖 AI yordamchi ishga tushdi!\n\n"
        "Menga nima kerakligini yozing (masalan: 'Telegram obunachi' yoki 'Instagram like').\n"
        "Bekor qilish uchun /cancel yozing.",
    )
    await state.set_state(OrderState.waiting_for_search)
    await callback.answer()


@dp.message(OrderState.waiting_for_search)
async def process_ai_search(message: types.Message, state: FSMContext):
    user_query = message.text
    async with aiohttp.ClientSession() as session:
        services = await fetch_services(session)

    if not services:
        await message.answer("Hozirda xizmatlar ro'yxati bo'sh.")
        await state.clear()
        return

    valid_ids = {s.get("service") for s in services}
    services_text = "\n".join(f"ID: {s.get('service')} | Nomi: {s.get('name')}" for s in services)

    prompt = f"""Siz SMM xizmatlari botining yordamchisiz. Foydalanuvchi quyidagi xizmatni qidirmoqda: "{user_query}"
Mavjud xizmatlar ro'yxatidan eng mos keladigan 3 tagacha xizmatni tanlang.
Faqatgina mos keladigan xizmatlarning ID raqamlarini vergul bilan ajratib yozing (masalan: 974,125). Topilmasa 0 deb yozing. Boshqa hech qanday matn yozmang.

Mavjud xizmatlar:
{services_text}"""

    try:
        response = None
        last_error = None
        for attempt in range(2):  # one retry for transient 503 "high demand" errors
            try:
                response = await asyncio.to_thread(
                    genai_client.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=prompt,
                )
                break
            except Exception as e:
                last_error = e
                if "UNAVAILABLE" in str(e) or "503" in str(e):
                    await asyncio.sleep(2)
                    continue
                raise
        if response is None:
            raise last_error

        ai_response = (response.text or "").strip()

        found_ids = []
        for word in ai_response.replace(",", " ").split():
            if word.isdigit():
                num = int(word)
                if num in valid_ids:
                    found_ids.append(num)

        if not found_ids:
            await message.answer("❌ Kechirasiz, bu so'rov bo'yicha hech narsa topilmadi. Kategoriyalardan foydalanib ko'ring.")
            await state.clear()
            return

        keyboard_buttons = []
        for s in services:
            if s.get("service") in found_ids:
                s_id = s.get("service")
                s_name = s.get("name", "Nomsiz xizmat")
                s_rate = s.get("rate")
                btn_text = f"{s_name[:25]} (1000 ta) — {s_rate} so'm"
                keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"srv_{s_id}")])

        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="back_home")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        await message.answer("✨ AI siz uchun topgan xizmatlar va ularning narxlari:", reply_markup=keyboard)
        await state.clear()

    except Exception as e:
        logger.exception("AI qidiruv xatosi")
        if "UNAVAILABLE" in str(e) or "503" in str(e):
            await message.answer(
                "⏳ AI xizmati hozir band (Google tomonidan yuqori talab). "
                "Birozdan so'ng qayta urinib ko'ring yoki kategoriyalardan foydalaning."
            )
        else:
            await message.answer("❌ Qidirishda xatolik yuz berdi. Iltimos, kategoriyalardan foydalaning.")
        await state.clear()


@dp.callback_query(F.data.startswith("srv_"))
async def select_service(callback: types.CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])

    async with aiohttp.ClientSession() as session:
        services = await fetch_services(session)
    service = next((s for s in services if s.get("service") == service_id), None)

    if service is None:
        await callback.answer("Bu xizmat topilmadi, qaytadan tanlang.", show_alert=True)
        return

    await state.update_data(
        service_id=service_id,
        service_name=service.get("name", "Nomsiz xizmat"),
        service_rate=service.get("rate"),
        min_qty=int(service.get("min", 1) or 1),
        max_qty=int(service.get("max", 1_000_000) or 1_000_000),
    )

    await callback.message.answer(
        "Iltimos, kanal yoki sahifa havolasini (link) yuboring.\nBekor qilish uchun /cancel yozing.",
    )
    await state.set_state(OrderState.waiting_for_link)
    await callback.answer()


@dp.message(OrderState.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.startswith(("http://", "https://", "@")):
        await message.answer("Bu havolaga o'xshamayapti. Iltimos, to'liq link yuboring (https://... ko'rinishida).")
        return

    await state.update_data(link=link)
    data = await state.get_data()
    await message.answer(
        f"Nechta miqdor kerakligini raqamda kiriting.\n"
        f"Ruxsat etilgan oraliq: {data['min_qty']} – {data['max_qty']}"
    )
    await state.set_state(OrderState.waiting_for_quantity)


@dp.message(OrderState.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if not (message.text or "").isdigit():
        await message.answer("Iltimos, faqat raqam kiriting!")
        return

    quantity = int(message.text)
    data = await state.get_data()

    if not (data["min_qty"] <= quantity <= data["max_qty"]):
        await message.answer(
            f"Miqdor {data['min_qty']} dan {data['max_qty']} gacha bo'lishi kerak. Qaytadan kiriting:"
        )
        return

    est_price = round((data["service_rate"] or 0) * quantity / 1000, 2)
    await state.update_data(quantity=quantity, est_price=est_price)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="confirm_order")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_order")],
        ]
    )
    await message.answer(
        "Buyurtmangizni tekshiring:\n\n"
        f"Xizmat: {data['service_name']}\n"
        f"Havola: {data['link']}\n"
        f"Miqdor: {quantity}\n"
        f"Taxminiy narx: {est_price} so'm\n\n"
        "Tasdiqlaysizmi?",
        reply_markup=keyboard,
    )
    await state.set_state(OrderState.waiting_for_confirm)


@dp.callback_query(OrderState.waiting_for_confirm, F.data == "cancel_order")
async def cancel_order(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("❎ Buyurtma bekor qilindi.", reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.callback_query(OrderState.waiting_for_confirm, F.data == "confirm_order")
async def confirm_order(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()

    async with aiohttp.ClientSession() as session:
        result = await create_topsmm_order(session, data["service_id"], data["link"], data["quantity"])

    if result and isinstance(result, dict) and "order" in result:
        await callback.message.answer(f"✅ Buyurtma qabul qilindi! ID raqami: {result['order']}")
    else:
        error_msg = result.get("error", "Noma'lum xatolik") if isinstance(result, dict) else "Ulanish xatosi"
        await callback.message.answer(f"❌ Xatolik yuz berdi: {error_msg}")

    await state.clear()
    await callback.answer()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
