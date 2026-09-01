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


# Buyurtma jarayoni uchun holatlar (FSM)
class OrderState(StatesGroup):
    waiting_for_link = State()
    waiting_for_quantity = State()


# Topsmm API'dan xizmatlarni olib, narxiga 25% ustama qo'shish funksiyasi
def get_custom_services():
    data = {"key": TOPSMM_API_KEY, "action": "services"}
    try:
        response = requests.post(TOPSMM_URL, data=data)
        services = response.json()
        
        # Agar javob ro'yxat ko'rinishida kelsa, har birining narxiga 25% qo'shamiz
        if isinstance(services, list):
            for service in services:
                base_rate = float(service.get("rate", 0))
                # 25% ustama qo'shish (1.25 ga ko'paytirish)
                service["rate"] = round(base_rate * 1.25, 2)
                
        return services
    except Exception as e:
        logging.error(f"Xizmatlarni olishda xatolik: {e}")
        return []


# Topsmm API'ga buyurtma yuborish funksiyasi
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


# /start buyrug'i
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Xizmatlar va Narxlar (+25%)", callback_data="show_services")]
        ]
    )
    await message.answer(
        "Salom! Botimizga xush kelibsiz.\n"
        "Barcha SMM xizmatlariga 25% ustama narx qo'yilgan.\n"
        "Xizmatlarni ko'rish uchun quyidagi tugmani bosing:",
        reply_markup=keyboard,
    )


# Xizmatlar ro'yxatini chiqarish va tanlash
@dp.callback_query(F.data == "show_services")
async def show_services(callback: types.CallbackQuery, state: FSMContext):
    services = get_custom_services()
    if not services:
        await callback.message.answer("Hozirda xizmatlar ro'yxatini olishda xatolik yuz berdi.")
        await callback.answer()
        return

    # Telegram xabari uzun bo'lib ketmasligi uchun dastlabki 10 ta xizmatni tugma shaklida chiqaramiz
    keyboard_buttons = []
    for s in services[:10]:
        s_id = s.get("service")
        s_name = s.get("name")
        s_rate = s.get("rate")
        
        # Tugma matni
        btn_text = f"{s_name[:30]}... ({s_rate})"
        keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"srv_{s_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer(
        "Quyidagi xizmatlardan birini tanlang (Narxlarga 25% foyiz qo'shilgan):",
        reply_markup=keyboard
    )
    await callback.answer()


# Xizmat tanlanganda
@dp.callback_query(F.data.startswith("srv_"))
async def select_service(callback: types.CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])
    await state.update_data(service_id=service_id)

    await callback.message.answer(
        "Iltimos, kanal yoki guruhingiz havolasini yuboring (masalan: `https://t.me/kanal_nomi`):",
        parse_mode="Markdown",
    )
    await state.set_state(OrderState.waiting_for_link)
    await callback.answer()


# Havolani qabul qilish
@dp.message(OrderState.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer(
        "Endi nechta miqdor kerakligini raqamda kiriting (masalan: `100`):"
    )
    await state.set_state(OrderState.waiting_for_quantity)


# Miqdorni qabul qilish va API'ga jo'natish
@dp.message(OrderState.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Iltimos, faqat raqam kiriting!")
        return

    quantity = int(message.text)
    data = await state.get_data()

    service_id = data["service_id"]
    link = data["link"]

    # Topsmm API ga buyurtma yuborish
    result = create_topsmm_order(service_id, link, quantity)

    if result and isinstance(result, dict) and "order" in result:
        await message.answer(
            f"✅ Buyurtma muvaffaqiyatli qabul qilindi!\n"
            f"🆔 Buyurtma ID raqami: {result['order']}"
        )
    else:
        error_msg = (
            result.get("error", "Noma'lum xatolik")
            if isinstance(result, dict)
            else "Ulanishda xatolik"
        )
        await message.answer(f"❌ Xatolik yuz berdi: {error_msg}")

    await state.clear()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
