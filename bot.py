import asyncio
import logging
import os
import requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ============ SOZLAMALAR ============
# Token va API kalitlar endi kod ichida emas, Render'ning
# "Environment" bo'limidan olinadi (xavfsizroq).
BOT_TOKEN = os.environ["BOT_TOKEN"]
TOPSMM_API_KEY = os.environ["TOPSMM_API_KEY"]
TOPSMM_URL = "https://topsmm.uz/api/v2"

MARKUP = 1.25  # 25% ustama

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Xizmatlar ro'yxatini xotirada saqlab turamiz (har safar API'ga
# so'rov yubormaslik uchun). 10 daqiqada bir marta yangilanadi.
_services_cache = {"data": None, "ts": 0}
CACHE_TTL = 600  # soniya


class OrderState(StatesGroup):
    waiting_for_link = State()
    waiting_for_quantity = State()
    waiting_for_confirm = State()


def get_custom_services(force_refresh: bool = False):
    """Xizmatlar ro'yxatini oladi, narxga 25% ustama qo'shadi, keshlaydi."""
    import time

    now = time.time()
    if not force_refresh and _services_cache["data"] is not None:
        if now - _services_cache["ts"] < CACHE_TTL:
            return _services_cache["data"]

    data = {"key": TOPSMM_API_KEY, "action": "services"}
    try:
        response = requests.post(TOPSMM_URL, data=data, timeout=15)
        services = response.json()

        if isinstance(services, list):
            for service in services:
                try:
                    base_rate = float(service.get("rate", 0))
                except (TypeError, ValueError):
                    base_rate = 0
                service["rate"] = round(base_rate * MARKUP, 2)
            _services_cache["data"] = services
            _services_cache["ts"] = now
            return services
        return []
    except Exception as e:
        logging.error(f"Xizmatlarni olishda xatolik: {e}")
        # API vaqtincha ishlamasa ham eski keshni qaytaramiz (bo'lsa)
        return _services_cache["data"] or []


def get_service_by_id(service_id: int):
    for s in get_custom_services():
        if int(s.get("service")) == service_id:
            return s
    return None


def create_topsmm_order(service_id, link, quantity):
    data = {
        "key": TOPSMM_API_KEY,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }
    try:
        response = requests.post(TOPSMM_URL, data=data, timeout=15)
        return response.json()
    except Exception as e:
        logging.error(f"Buyurtma xatosi: {e}")
        return None


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Xizmatlar bo'limi", callback_data="show_categories")]
        ]
    )
    await message.answer(
        "Salom! SMM xizmatlari botiga xush kelibsiz.\n"
        "Buyurtma berish uchun quyidagi tugmani bosing:",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "show_categories")
async def show_categories(callback: types.CallbackQuery):
    services = get_custom_services()
    if not services:
        await callback.message.answer("⚠️ Hozirda xizmatlar topilmadi. Birozdan so'ng qayta urinib ko'ring.")
        await callback.answer()
        return

    categories = sorted(set(s.get("category", "Boshqa") for s in services))

    keyboard_buttons = [
        [InlineKeyboardButton(text=cat, callback_data=f"cat_{i}")]
        for i, cat in enumerate(categories)
    ]
    # Kategoriya nomlarini indeks orqali saqlaymiz (callback_data 64 belgidan oshmasligi uchun)
    dp["_categories"] = categories  # oddiy global saqlash

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Ijtimoiy tarmoq yoki bo'limni tanlang:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("cat_"))
async def show_services_in_category(callback: types.CallbackQuery):
    idx = int(callback.data.replace("cat_", ""))
    categories = dp.get("_categories", [])
    if idx >= len(categories):
        await callback.message.answer("Kategoriya topilmadi, qaytadan urinib ko'ring: /start")
        await callback.answer()
        return

    selected_cat = categories[idx]
    services = get_custom_services()

    keyboard_buttons = []
    for s in services:
        if s.get("category") == selected_cat:
            s_id = s.get("service")
            s_name = s.get("name")
            price = s.get("rate")
            label = f"{s_name[:35]} — {price} so'm/1000"
            keyboard_buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"srv_{s_id}")])

    if not keyboard_buttons:
        await callback.message.answer("Bu bo'limda xizmatlar topilmadi.")
        await callback.answer()
        return

    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_categories")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Kerakli xizmatni tanlang:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("srv_"))
async def select_service(callback: types.CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])
    service = get_service_by_id(service_id)

    if not service:
        await callback.message.answer("Xizmat topilmadi, qaytadan urinib ko'ring: /start")
        await callback.answer()
        return

    await state.update_data(service_id=service_id)

    min_q = service.get("min", "—")
    max_q = service.get("max", "—")
    price = service.get("rate", 0)

    await callback.message.answer(
        f"📦 *{service.get('name')}*\n"
        f"💰 Narx: {price} so'm / 1000 ta\n"
        f"📊 Min: {min_q}  |  Max: {max_q}\n\n"
        "Iltimos, kanal yoki sahifa havolasini (link) yuboring:",
        parse_mode="Markdown",
    )
    await state.set_state(OrderState.waiting_for_link)
    await callback.answer()


@dp.message(OrderState.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    link = message.text.strip()
    if not link.startswith("http"):
        await message.answer("⚠️ Iltimos, to'g'ri havola (link) yuboring, masalan: https://...")
        return

    await state.update_data(link=link)
    await message.answer("Nechta miqdor kerakligini raqamda kiriting (masalan: `100`):", parse_mode="Markdown")
    await state.set_state(OrderState.waiting_for_quantity)


@dp.message(OrderState.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqam kiriting!")
        return

    quantity = int(message.text)
    data = await state.get_data()
    service = get_service_by_id(data.get("service_id"))

    if not service:
        await message.answer("Xizmat topilmadi. Qaytadan boshlang: /start")
        await state.clear()
        return

    min_q = int(service.get("min", 0) or 0)
    max_q = int(service.get("max", 0) or 0)

    if min_q and quantity < min_q:
        await message.answer(f"⚠️ Minimal miqdor: {min_q}. Iltimos, kattaroq son kiriting.")
        return
    if max_q and quantity > max_q:
        await message.answer(f"⚠️ Maksimal miqdor: {max_q}. Iltimos, kichikroq son kiriting.")
        return

    price_per_1000 = float(service.get("rate", 0))
    total_price = round(price_per_1000 * quantity / 1000, 2)

    await state.update_data(quantity=quantity, total_price=total_price)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="confirm_order"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_order"),
            ]
        ]
    )
    await message.answer(
        f"🧾 *Buyurtma tafsilotlari:*\n"
        f"Miqdor: {quantity}\n"
        f"Jami narx: {total_price} so'm\n\n"
        "Tasdiqlaysizmi?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    await state.set_state(OrderState.waiting_for_confirm)


@dp.callback_query(F.data == "cancel_order", OrderState.waiting_for_confirm)
async def cancel_order(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("❌ Buyurtma bekor qilindi.")
    await callback.answer()


@dp.callback_query(F.data == "confirm_order", OrderState.waiting_for_confirm)
async def confirm_order(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()

    required = ("service_id", "link", "quantity")
    if not all(k in data for k in required):
        await callback.message.answer("⚠️ Ma'lumot yetarli emas. Qaytadan boshlang: /start")
        await state.clear()
        await callback.answer()
        return

    result = create_topsmm_order(data["service_id"], data["link"], data["quantity"])

    if result and isinstance(result, dict) and "order" in result:
        await callback.message.answer(f"✅ Buyurtma qabul qilindi! ID raqami: {result['order']}")
    else:
        error_msg = result.get("error", "Noma'lum xatolik") if isinstance(result, dict) else "Ulanish xatosi"
        await callback.message.answer(f"❌ Xatolik yuz berdi: {error_msg}")

    await state.clear()
    await callback.answer()


async def main():
    logging.info("Bot ishga tushmoqda...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
