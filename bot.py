import asyncio
import difflib
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

# TopSMM narxlari RUB da qaytadi. Fix kurs bilan so'mga o'giramiz.
# Kursni yangilash uchun .env dagi RUB_TO_UZS_RATE ni tahrirlang.
RUB_TO_UZS_RATE = float(os.environ.get("RUB_TO_UZS_RATE", "135"))

# Balansni to'ldirish so'rovlari shu admin(lar)ga yuboriladi (Telegram user id).
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", os.environ.get("ADMIN_ID", "")).split(",") if x.strip()
}
# To'lov uchun foydalanuvchiga ko'rsatiladigan admin username (@ belgisiz yozing, .env da).
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").lstrip("@")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# In-memory caches (fine for a single-process bot; use Redis/DB if you scale
# out or need the data to survive a restart).
_services_cache: dict = {"data": [], "fetched_at": 0}
_category_lookup: dict[str, str] = {}   # short id -> full category name
_user_balances: dict[int, float] = {}   # user_id -> balans (so'm)
_pending_topups: dict[str, dict] = {}   # topup_id -> {user_id, amount, username}


class OrderState(StatesGroup):
    waiting_for_search = State()
    waiting_for_link = State()
    waiting_for_quantity = State()
    waiting_for_confirm = State()
    waiting_for_topup_amount = State()


def short_id(text: str) -> str:
    """Stable short id for callback_data (Telegram limits callback_data to 64 bytes)."""
    return format(zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF, "x")


def get_balance(user_id: int) -> float:
    return _user_balances.get(user_id, 0.0)


async def fetch_services(session: aiohttp.ClientSession, force: bool = False) -> list:
    """Fetch services from TopSMM (rates in RUB), convert to so'm and apply markup.

    Cached for SERVICES_CACHE_TTL seconds.
    """
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
            base_rate_rub = float(service.get("rate", 0))
        except (TypeError, ValueError):
            base_rate_rub = 0.0
        # RUB -> UZS, keyin markup qo'shiladi. Natija "1000 ta narxi, so'm".
        service["rate"] = round(base_rate_rub * RUB_TO_UZS_RATE * MARKUP_MULTIPLIER, 2)

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
            [InlineKeyboardButton(text="💰 Balans", callback_data="show_balance")],
        ]
    )


def balance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Balansni to'ldirish", callback_data="topup_start")],
            [InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="back_home")],
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


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    bal = get_balance(message.from_user.id)
    await message.answer(f"💰 Joriy balansingiz: {bal:.2f} so'm", reply_markup=balance_keyboard())


@dp.callback_query(F.data == "show_balance")
async def show_balance(callback: types.CallbackQuery):
    bal = get_balance(callback.from_user.id)
    await callback.message.answer(f"💰 Joriy balansingiz: {bal:.2f} so'm", reply_markup=balance_keyboard())
    await callback.answer()


# --- BALANSNI TO'LDIRISH (admin tomonidan qo'lda tasdiqlanadi) ---
@dp.callback_query(F.data == "topup_start")
async def topup_start(callback: types.CallbackQuery, state: FSMContext):
    if not ADMIN_IDS:
        await callback.message.answer(
            "⚠️ To'lov tizimi hozircha sozlanmagan (admin belgilanmagan). Iltimos, keyinroq urinib ko'ring."
        )
        await callback.answer()
        return
    contact_hint = f" (to'lov rekvizitlari uchun @{ADMIN_USERNAME} bilan bog'lanasiz)" if ADMIN_USERNAME else ""
    await callback.message.answer(
        f"💳 Balansni qancha so'mga to'ldirmoqchisiz{contact_hint}? Miqdorni raqamda yozing.\n"
        "To'lovni admin tekshirib, tasdiqlagach balansingizga tushadi.\n"
        "Bekor qilish uchun /cancel yozing."
    )
    await state.set_state(OrderState.waiting_for_topup_amount)
    await callback.answer()


@dp.message(OrderState.waiting_for_topup_amount)
async def process_topup_amount(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Iltimos, musbat raqam kiriting (masalan: 50000).")
        return

    amount = float(text)
    user = message.from_user
    topup_id = short_id(f"{user.id}-{time.time()}")
    _pending_topups[topup_id] = {
        "user_id": user.id,
        "amount": amount,
        "username": user.username or user.full_name,
    }

    if ADMIN_USERNAME:
        contact_line = f"Karta raqami va to'lov rekvizitlari uchun @{ADMIN_USERNAME} ga yozing."
    else:
        contact_line = "Karta raqami va to'lov rekvizitlari uchun admin bilan bog'laning."

    await message.answer(
        "✅ So'rovingiz adminga yuborildi. Tasdiqlangach balansingizga mablag' tushadi.\n"
        f"{contact_line}",
        reply_markup=main_menu_keyboard(),
    )
    await state.clear()

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"topup_ok_{topup_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"topup_no_{topup_id}"),
            ]
        ]
    )
    admin_text = (
        "🆕 Balans to'ldirish so'rovi\n\n"
        f"Foydalanuvchi: @{_pending_topups[topup_id]['username']} (id: {user.id})\n"
        f"Miqdor: {amount:.2f} so'm"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=admin_keyboard)
        except Exception:
            logger.exception("Adminga (%s) xabar yuborib bo'lmadi", admin_id)


@dp.callback_query(F.data.startswith("topup_ok_") | F.data.startswith("topup_no_"))
async def handle_topup_decision(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Bu tugma faqat admin uchun.", show_alert=True)
        return

    approve = callback.data.startswith("topup_ok_")
    topup_id = callback.data.split("_", 2)[2]
    topup = _pending_topups.pop(topup_id, None)

    if topup is None:
        await callback.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        return

    user_id = topup["user_id"]
    amount = topup["amount"]

    if approve:
        _user_balances[user_id] = get_balance(user_id) + amount
        await callback.message.edit_text(callback.message.text + "\n\n✅ TASDIQLANDI")
        try:
            await bot.send_message(
                user_id,
                f"✅ Balansingiz {amount:.2f} so'mga to'ldirildi.\n"
                f"Joriy balans: {get_balance(user_id):.2f} so'm",
            )
        except Exception:
            logger.exception("Foydalanuvchiga (%s) xabar yuborib bo'lmadi", user_id)
    else:
        await callback.message.edit_text(callback.message.text + "\n\n❌ RAD ETILDI")
        try:
            await bot.send_message(user_id, "❌ Balansni to'ldirish so'rovingiz rad etildi. Admin bilan bog'laning.")
        except Exception:
            logger.exception("Foydalanuvchiga (%s) xabar yuborib bo'lmadi", user_id)

    await callback.answer("Ko'rib chiqildi.")


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
    groups = group_categories_by_platform(categories)

    # Platformalarni ma'lum tartibda, so'ng "Boshqa"ni oxirida ko'rsatamiz.
    ordered_keys = [p for p in KNOWN_PLATFORMS if p in groups]
    if OTHER_PLATFORM_KEY in groups:
        ordered_keys.append(OTHER_PLATFORM_KEY)

    keyboard_buttons = []
    for key in ordered_keys:
        count = len(groups[key])
        if key == OTHER_PLATFORM_KEY:
            label = f"📦 Boshqa ({count})"
        else:
            emoji = PLATFORM_EMOJI.get(key, "📂")
            label = f"{emoji} {key.capitalize()} ({count})"
        keyboard_buttons.append([InlineKeyboardButton(text=label, callback_data=f"plat_{key}")])

    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="back_home")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Avval platformani tanlang:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("plat_"))
async def show_categories_in_platform(callback: types.CallbackQuery):
    platform_key = callback.data.removeprefix("plat_")

    async with aiohttp.ClientSession() as session:
        services = await fetch_services(session)

    categories = sorted({s.get("category", "Boshqa") for s in services})
    groups = group_categories_by_platform(categories)
    cats = groups.get(platform_key, [])

    if not cats:
        await callback.answer("Bu platformada bo'limlar topilmadi.", show_alert=True)
        return

    keyboard_buttons = []
    for cat in cats:
        cid = short_id(cat)
        _category_lookup[cid] = cat  # exact lookup, no truncation collisions
        keyboard_buttons.append([InlineKeyboardButton(text=cat[:40], callback_data=f"cat_{cid}")])

    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_categories")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Bo'limni tanlang:", reply_markup=keyboard)
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


# O'zbekcha so'zlarni TopSMM'dagi odatiy ingliz/rus atamalariga moslashtirish uchun
# kichik lug'at — lokal qidiruv ko'proq holatda to'g'ri ishlashi uchun.
KEYWORD_SYNONYMS: dict[str, list[str]] = {
    "obunachi": ["subscriber", "subscribers", "sub"],
    "obuna": ["subscriber", "subscribers"],
    "layk": ["like", "likes"],
    "layklar": ["like", "likes"],
    "kuzatuvchi": ["follower", "followers"],
    "korish": ["view", "views"],
    "korishlar": ["view", "views"],
    "tomosha": ["view", "views"],
    "azo": ["member", "members"],
    "sharh": ["comment", "comments"],
    "izoh": ["comment", "comments"],
    "ulashish": ["share", "shares"],
    "reaksiya": ["reaction", "reactions"],
    "auditoriya": ["audience"],
    "reklama": ["promotion", "promo"],
}

# Mashhur platforma nomlari — foydalanuvchi imlo xatosi bilan yozsa
# ("Telgram", "Yutube") ham to'g'irlab olish uchun.
KNOWN_PLATFORMS = [
    "telegram", "instagram", "facebook", "tiktok", "youtube",
    "twitter", "whatsapp", "vkontakte", "spotify", "threads",
]

PLATFORM_EMOJI = {
    "telegram": "📱", "instagram": "📸", "facebook": "📘", "tiktok": "🎵",
    "youtube": "▶️", "twitter": "🐦", "whatsapp": "💬", "vkontakte": "🌐",
    "spotify": "🎧", "threads": "🧵",
}
OTHER_PLATFORM_KEY = "boshqa"


def group_categories_by_platform(categories: list) -> dict:
    """Kategoriyalarni platforma nomi bo'yicha guruhlaydi (Telegram, Instagram,
    TikTok, ...). Hech qaysi platformaga mos kelmagan kategoriyalar "Boshqa"
    guruhiga tushadi. Bu "Kategoriyalar" bo'limini bosqichma-bosqich, qidirish
    uchun qulayroq qiladi — foydalanuvchi o'nlab kategoriya o'rniga avval
    platformani, keyin o'sha platformaning bo'limlarini tanlaydi.
    """
    groups: dict = {}
    for cat in categories:
        cat_lower = cat.lower()
        matched = next((p for p in KNOWN_PLATFORMS if p in cat_lower), None)
        key = matched or OTHER_PLATFORM_KEY
        groups.setdefault(key, []).append(cat)
    return groups


def normalize_query_words(query: str) -> list[str]:
    """So'rov so'zlarini tozalaydi, imlo xatolarini tuzatadi va sinonimlar
    bilan kengaytiradi — lokal qidiruv ko'proq to'g'ri natija berishi uchun.
    """
    raw_words = [w.strip(".,!?'\u2019") for w in query.lower().split() if len(w.strip(".,!?'\u2019")) >= 2]
    expanded: set[str] = set()
    for w in raw_words:
        expanded.add(w)
        # Mashhur platforma nomiga imlosi yaqin bo'lsa, to'g'irlab qo'shamiz
        close = difflib.get_close_matches(w, KNOWN_PLATFORMS, n=1, cutoff=0.72)
        if close:
            expanded.add(close[0])
        if w in KEYWORD_SYNONYMS:
            expanded.update(KEYWORD_SYNONYMS[w])
    return list(expanded)


def prefilter_services(query: str, services: list, limit: int = 40) -> tuple[list, bool]:
    """So'rov so'zlariga qarab xizmatlarni tez, lokal tarzda saralaydi.

    Bu Gemini'ga yuboriladigan matnni (va shu bilan javob vaqtini) sezilarli
    kamaytiradi — butun ro'yxat o'rniga faqat eng mos ~40 ta xizmat yuboriladi.

    Ikkinchi qiymat — kamida bitta xizmat kalit so'z bo'yicha haqiqatan ham
    mos kelganini bildiradi (True/False). Bu keyinchalik "ehtimoliy mos"
    takliflarni faqat mazmunli bo'lganda ko'rsatish, va mos kelmasa Gemini'ga
    kesilgan emas, TO'LIQ ro'yxatni yuborish uchun ishlatiladi.
    """
    query_words = normalize_query_words(query)
    if not query_words:
        return services, False

    scored = []
    for s in services:
        name = str(s.get("name", "")).lower()
        category = str(s.get("category", "")).lower()
        haystack = f"{name} {category}"
        score = 0
        for w in query_words:
            if w in haystack:
                score += 2
            if w in name:
                score += 1  # nomida to'g'ridan-to'g'ri uchrashi ustuvor
        if score:
            scored.append((score, s))

    if not scored:
        # Kalit so'z bo'yicha hech narsa topilmadi (masalan tillar mos
        # kelmadi) — Gemini'ga TO'LIQ ro'yxatni beramiz, chunki u
        # semantik jihatdan tushunishi mumkin, faqat qisqartirilgan
        # (tasodifiy) qism kerakli xizmatlarni chetlab o'tishi mumkin edi.
        return services, False

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]], True


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

    # 1) Lokal tez filtrlash — Gemini'ga yuboriladigan hajmni kamaytiradi.
    candidates, had_local_match = prefilter_services(user_query, services, limit=40)
    services_text = "\n".join(f"ID: {s.get('service')} | Nomi: {s.get('name')}" for s in candidates)

    prompt = f"""Foydalanuvchi qidirmoqda: "{user_query}"
Quyidagi ro'yxatdan eng mos 3 tagacha xizmatning ID raqamini vergul bilan yozing (masalan: 974,125).
Aniq mos kelmasa ham, mavzu yoki maqsad jihatidan eng yaqin bo'lgan xizmatlarni ehtimollik sifatida taklif qiling —
faqat ro'yxatda mutlaqo aloqador hech narsa bo'lmasagina 0 deb yozing. Boshqa hech narsa yozmang.

{services_text}"""

    try:
        response = None
        last_error = None
        for attempt in range(2):  # one retry for transient 503 "high demand" errors
            try:
                # 2) Qisqa, deterministik javob so'raymiz (faqat ID'lar kerak) —
                #    bu Gemini javobini tezlashtiradi.
                # 3) Timeout — Gemini sekinlashsa foydalanuvchi cheksiz kutmaydi.
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        genai_client.models.generate_content,
                        model=GEMINI_MODEL,
                        contents=prompt,
                        config={
                            "temperature": 0,
                            "max_output_tokens": 100,
                            # Modelning ichki "thinking" bosqichini o'chiramiz —
                            # aks holda u token byudjetini fikrlashga sarflab,
                            # haqiqiy javobga (ID raqamlariga) ulgurmasligi
                            # mumkin edi, natijada bo'sh javob qaytardi.
                            "thinking_config": {"thinking_budget": 0},
                        },
                    ),
                    timeout=10,
                )
                break
            except asyncio.TimeoutError as e:
                last_error = e
                continue  # keyingi urinishga o'tamiz (jami 2 marta)
            except Exception as e:
                last_error = e
                if "UNAVAILABLE" in str(e) or "503" in str(e):
                    await asyncio.sleep(1)
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
            if had_local_match:
                # AI aniq javob topa olmadi, lekin lokal saralashda
                # kalit so'zga yaqin xizmatlar bor — shularni "ehtimol
                # mos" sifatida taklif qilamiz, mutlaqo bo'sh javob
                # o'rniga.
                fallback_ids = [s.get("service") for s in candidates[:3]]
                keyboard_buttons = []
                for s in services:
                    if s.get("service") in fallback_ids:
                        s_id = s.get("service")
                        s_name = s.get("name", "Nomsiz xizmat")
                        s_rate = s.get("rate")
                        btn_text = f"{s_name[:25]} (1000 ta) — {s_rate} so'm"
                        keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"srv_{s_id}")])
                keyboard_buttons.append([InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="back_home")])
                keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
                await message.answer(
                    "🤔 Aniq mos xizmat topilmadi, lekin so'rovingizga yaqin bo'lganlar shular bo'lishi mumkin:",
                    reply_markup=keyboard,
                )
            else:
                await message.answer(
                    "❌ Kechirasiz, bu so'rov bo'yicha hech narsa topilmadi. Kategoriyalardan foydalanib ko'ring.",
                    reply_markup=main_menu_keyboard(),
                )
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
        if isinstance(e, asyncio.TimeoutError):
            await message.answer(
                "⏳ AI javob berishga vaqt oldi. Iltimos, qaytadan urinib ko'ring yoki kategoriyalardan foydalaning.",
                reply_markup=main_menu_keyboard(),
            )
        elif "UNAVAILABLE" in str(e) or "503" in str(e):
            await message.answer(
                "⏳ AI xizmati hozir band (Google tomonidan yuqori talab). "
                "Birozdan so'ng qayta urinib ko'ring yoki kategoriyalardan foydalaning.",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await message.answer(
                "❌ Qidirishda xatolik yuz berdi. Iltimos, kategoriyalardan foydalaning.",
                reply_markup=main_menu_keyboard(),
            )
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

    balance = get_balance(message.from_user.id)
    if balance < est_price:
        await message.answer(
            "⚠️ Balansingiz yetarli emas.\n\n"
            f"Kerak: {est_price:.2f} so'm\n"
            f"Joriy balans: {balance:.2f} so'm\n"
            f"Yetishmayapti: {est_price - balance:.2f} so'm\n\n"
            "Avval balansni to'ldiring, keyin buyurtmani qaytadan bering.",
            reply_markup=balance_keyboard(),
        )
        await state.clear()
        return

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
        f"Narx: {est_price:.2f} so'm (balansdan yechiladi)\n"
        f"Joriy balans: {balance:.2f} so'm\n\n"
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
    user_id = callback.from_user.id
    est_price = data["est_price"]

    # Balansni yana bir bor tekshiramiz (foydalanuvchi shu orada boshqa
    # buyurtma bergan yoki balans o'zgargan bo'lishi mumkin) va darhol
    # yechib qo'yamiz — bu double-spend'ning oldini oladi.
    balance = get_balance(user_id)
    if balance < est_price:
        await callback.message.answer(
            "⚠️ Balansingiz yetarli emas. Buyurtma bekor qilindi.",
            reply_markup=balance_keyboard(),
        )
        await state.clear()
        await callback.answer()
        return

    _user_balances[user_id] = balance - est_price

    async with aiohttp.ClientSession() as session:
        result = await create_topsmm_order(session, data["service_id"], data["link"], data["quantity"])

    if result and isinstance(result, dict) and "order" in result:
        await callback.message.answer(
            f"✅ Buyurtma qabul qilindi! ID raqami: {result['order']}\n"
            f"Balansdan yechildi: {est_price:.2f} so'm\n"
            f"Qolgan balans: {get_balance(user_id):.2f} so'm"
        )
    else:
        # Buyurtma muvaffaqiyatsiz — yechilgan mablag'ni qaytaramiz.
        _user_balances[user_id] = get_balance(user_id) + est_price
        error_msg = result.get("error", "Noma'lum xatolik") if isinstance(result, dict) else "Ulanish xatosi"
        await callback.message.answer(
            f"❌ Xatolik yuz berdi: {error_msg}\n"
            f"Mablag' balansingizga qaytarildi. Joriy balans: {get_balance(user_id):.2f} so'm"
        )

    await state.clear()
    await callback.answer()


# Yuqoridagi hech qaysi handlerga mos kelmagan har qanday xabar shu yerga
# tushadi (masalan foydalanuvchi biror aktiv holatsiz erkin matn yozsa).
# Botni "jim" qoldirmaslik va foydalanuvchini asosiy menyuga qaytarish uchun.
@dp.message()
async def fallback_message(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Kechirasiz, buni tushunmadim. Quyidagi menyudan foydalaning:",
        reply_markup=main_menu_keyboard(),
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
