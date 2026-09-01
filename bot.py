import asyncio
import logging
import requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Sozlamalar
BOT_TOKEN = "8795349150:AAGYRemHODLao7MBqBn5fGThY2Xx9Iduc9w"
TOPSMM_API_KEY = "f58aa981c6f257fa343973e879923ba1"
TOPSMM_URL = "https://topsmm.uz/api/v2"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class OrderState(StatesGroup):
    waiting_for_link = State()
    waiting_for_quantity = State()


# Xizmatlarni olish va 25% yashirin ustama qo'shish
def get_custom_services():
    data = {"key": TOPSMM_API_KEY, "action": "services"}
    try:
        response = requests.post(TOPSMM_URL, data=data)
        services = response.json()
        
        if isinstance(services, list):
            for service in services:
                base_rate = float(service.get("rate", 0))
                service["rate"] = round(base_rate * 1.25, 2)
                
        return services
    except Exception as e:
        logging.error(f"Xizmatlarni olishda xatolik: {e}")
        return []


def create_topsmm_order(service_id, link, quantity):
    data = {
        "key": TOPSMM_API_KEY,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }
    try:
        response = requests.post(TOPSMM_URL, data=data)
        return response.json()
    except Exception as e:
        logging.error(f"Buyurtma xatosi: {e}")
        return None


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Xizmatlar kategoriyasi", callback_data="show_categories")]
        ]
    )
    await message.answer(
        "Salom! Botimizga xush kelibsiz.\n"
        "Xizmatlardan foydalanish uchun quyidagi tugmani bosing:",
        reply_markup=keyboard,
    )


# Kategoriyalarni chiqarish
@dp.callback_query(F.data == "show_categories")
async def show_categories(callback: types.CallbackQuery):
    services = get_custom_services()
    if not services:
        await callback.message.answer("Hozirda xizmatlar topilmadi.")
        await callback.answer()
        return

    # Unikal kategoriyalarni yig'amiz
    categories = sorted(list(set(s.get("category", "Boshqa") for s in services)))

    keyboard_buttons = []
    for cat in categories:
        # Har bir kategoriya tugmasi
        keyboard_buttons.append([InlineKeyboardButton(text=cat, callback_data=f"cat_{cat[:30]}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Kerakli bo'limni (kategoriyani) tanlang:", reply_markup=keyboard)
    await callback.answer()


# Tanlangan kategoriya ichidagi xizmatlarni chiqarish
@dp.callback_query(F.data.startswith("cat_"))
async def show_services_in_category(callback: types.CallbackQuery):
    selected_cat = callback.data.replace("cat_", "")
    services = get_custom_services()

    keyboard_buttons = []
    for s in services:
        if s.get("category", "").startswith(selected_cat):
            s_id = s.get("service")
            s_name = s.get("name")
            # Narx ko'rsatilmaydi, faqat xizmat nomi
            keyboard_buttons.append([InlineKeyboardButton(text=s_name[:45], callback_data=f"srv_{s_id}")])

    if not keyboard_buttons:
        await callback.message.answer("Bu bo'limda xizmatlar topilmadi.")
        await callback.answer()
        return

    # Orqaga qaytish tugmasini qo'shamiz
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_categories")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Xizmatni tanlang:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("srv_"))
async def select_service(callback: types.CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])
    await state.update_data(service_id=service_id)

    await callback.message.answer(
        "Iltimos, kerakli havola (link) ni yuboring:",
        parse_mode="Markdown",
    )
    await state.set_state(OrderState.waiting_for_link)
    await callback.answer()


@dp.message(OrderState.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("Nechta miqdor kerakligini raqamda kiriting (masalan: `100`):")
    await state.set_state(OrderState.waiting_for_quantity)


@dp.message(OrderState.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Iltimos, faqat raqam kiriting!")
        return

    quantity = int(message.text)
    data = await state.get_data()

    result = create_topsmm_order(data["service_id"], data["link"], quantity)

    if result and isinstance(result, dict) and "order" in result:
        await message.answer(f"✅ Buyurtma qabul qilindi! ID: {result['order']}")
    else:
        error_msg = result.get("error", "Xatolik") if isinstance(result, dict) else "Ulanish xatosi"
        await message.answer(f"❌ Xatolik: {error_msg}")

    await state.clear()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
