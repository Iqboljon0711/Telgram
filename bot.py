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
TOPSMM_API_KEY = "SIZNING_TOPSMM_API_KALITINGIZ"  # <--- Bu yerga Topsmm.uz dan olgan API kalitingizni yozing
TOPSMM_URL = "https://topsmm.uz/api/v2"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# Buyurtma jarayoni uchun holatlar (FSM)
class OrderState(StatesGroup):
  waiting_for_link = State()
  waiting_for_quantity = State()


# Topsmm API'dan xizmatlarni olish funksiyasi
def get_topsmm_services():
  data = {"key": TOPSMM_API_KEY, "action": "services"}
  try:
    response = requests.post(TOPSMM_URL, data=data)
    return response.json()
  except Exception as e:
    logging.error(f"API xatosi: {e}")
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
  keyboard = InlineKeyboardMarkup(inline_keyboard=[
      [
          InlineKeyboardButton(
              text="📦 Buyurtma berish", callback_data="show_services"
          )
      ]
  ])
  await message.answer(
      "Salom! Botimizga xush kelibsiz.\n"
      "SMM xizmatlaridan foydalanish uchun quyidagi tugmani bosing:",
      reply_markup=keyboard,
  )


# Xizmatni tanlash bosqichi (Misol tariqasida 974-xizmat olingan)
@dp.callback_query(F.data == "show_services")
async def show_services(callback: types.CallbackQuery, state: FSMContext):
  service_id = 974
  service_name = "100% Telegram uz Ozbek Aktiv Obunachi"

  await state.update_data(service_id=service_id, service_name=service_name)

  await callback.message.answer(
      f"Tanlangan xizmat: **{service_name}**\n\n"
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
      "Endi nechta obunachi kerakligini raqamda kiriting (masalan: `100`):"
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

  # Topsmm API ga so'rov yuborish
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
    await message.answer(
        f"❌ Xatolik yuz berdi: {error_msg}\n"
        "(Iltimos, Topsmm.uz balansingiz yetarli ekanligini va API kalitingiz to'g'riligini tekshiring)"
    )

  await state.clear()


async def main():
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
