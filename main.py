
from dotenv import load_dotenv
import asyncio
import json
import logging
import os
import random
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import AsyncOpenAI


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
RECIPES_PATH = BASE_DIR / "recipes.json"

with RECIPES_PATH.open("r", encoding="utf-8") as f:
    RECIPES = json.load(f)

if not isinstance(RECIPES, list) or not RECIPES:
    raise RuntimeError("recipes.json must contain a non-empty list of recipes")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

TRY_NEXT_BUTTON_TEXT = "Нова страва"

MAIN_KEYBOARD = types.ReplyKeyboardMarkup(
    keyboard=[[types.KeyboardButton(text=TRY_NEXT_BUTTON_TEXT)]],
    resize_keyboard=True,
    one_time_keyboard=False,
)


class QuizStates(StatesGroup):
    waiting_for_answer = State()


openai_client: AsyncOpenAI | None = None


async def start_handler(message: types.Message) -> None:
    """
    Handle /start command and show the main button.
    """
    await message.answer(
        "Бот, який допоможе тобі вивчити всі рецепти.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_try_next(message: types.Message, state: FSMContext) -> None:
    """
    Handle pressing the 'Try next' button:
    - pick a random dish
    - ask user to write its recipe
    """
    dish = random.choice(RECIPES)

    # Store current dish in state for future extensions (e.g. checking answer)
    await state.update_data(current_dish_name=dish.get("name"))
    await state.set_state(QuizStates.waiting_for_answer)

    await message.answer(
        f"Страва: {dish.get('name')}\n\nБудь ласка, напишіть його рецепт.",
        reply_markup=MAIN_KEYBOARD,
    )


async def evaluate_answer_with_model(
    dish_name: str, official_recipe: str, user_recipe: str, price: str, weight: str
) -> str:
    """
    Use OpenAI model to compare user's recipe with the official one and rate it.
    """
    if openai_client is None:
        return "Evaluation service is temporarily unavailable. Please try another dish later."

    prompt = """
    Ти – експерт-шеф-кухар, який проводить тести для відбору кухарів у ресторан.

    У своїй відповіді можеш звертатися безпосередньо до кандидата. Наприклад: "Ти написав...", "Тобі треба...".

    Правила:
    1) Спілкуйся тільки українською, навіть якщо рецепт кандидата написаний російською.
    2) Порівняй рецепт кандидата з офіційним і коротко оцінюй, наскільки вони збігаються.
    3) Формат відповіді — стисло, у стилі Telegram: 4–5 коротких речень.
    4) Виділяй головні розбіжності, пропуски або помилки.
    5) Завжди додавай окремим рядком: 📍 Оцінка: X/10 (ціле число, де 10 = майже ідентичний).
    6) Також я надам тобі інформацію про вартість страви (в українських гривнях) та її вагу (у грамах). Можеш використовувати цю інформацію у своїй генерації. Наприклад, вивести інформацію після оцінки у вигляді (кожен параметр з нового рядка): 💵Ціна: <price>грн. \n ⚖️Вага: <weight>г.
    7) (ОПЦІОНАЛЬНО) Якщо можливо, дай одну дуже коротку, практичну пораду, як краще запам’ятати саме цей рецепт (без абстракцій). Відокрем її від основного тексту ньюлайнами та кількома тире (---)
    
    Вхідні дані:
    - Назва страви: {dish_name}
    - Офіційний рецепт: {official_recipe}
    - Рецепт кандидата: {user_recipe}
    - Вартість (у гривнях): {price}
    - Вага (у грамах): {weight}

    Завдання:
    Проаналізуй та сформуй підсумок згідно з правилами.
    """
    prompt = prompt.format(
        dish_name=dish_name, official_recipe=official_recipe, user_recipe=user_recipe, price=price, weight=weight
    )
    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "Ви лаконічний, суворий оцінювач рецептів.",
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=200,
        temperature=0.3,
    )

    return (response.choices[0].message.content or "").strip()


async def handle_answer(message: types.Message, state: FSMContext) -> None:
    """
    Handle user's recipe answer.
    """
    data = await state.get_data()
    dish_name = data.get("current_dish_name")

    dish = None
    if dish_name:
        dish = next((d for d in RECIPES if d.get("name") == dish_name), None)

    if not dish:
        # Fallback if we, for some reason, lost the dish in state
        await message.answer(
            "I couldn't find the official recipe this time, but you can try another dish.",
            reply_markup=MAIN_KEYBOARD,
        )
        await state.clear()
        return

    official_recipe = dish.get("recipe") or "No recipe description available."
    user_recipe = message.text or ""

    evaluation = await evaluate_answer_with_model(
        dish.get("name", "Unknown dish"),
        official_recipe,
        user_recipe,
        dish.get("price", "Unknown price"),
        dish.get("weight", "Unknown weight"),
    )

    image_url = dish.get("image_url")

    # Send only model response and image (if available)
    if image_url:
        await message.answer_photo(
            photo=image_url,
            caption=evaluation,
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await message.answer(evaluation, reply_markup=MAIN_KEYBOARD)

    # Clear state so user can start a new round by pressing the button again
    await state.clear()


async def main() -> None:
    """
    Entry point for the Telegram bot.
    """
    # Basic logging setup
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in the environment")

    global openai_client
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Register handlers
    dp.message.register(start_handler, CommandStart())
    dp.message.register(handle_try_next, F.text == TRY_NEXT_BUTTON_TEXT)
    dp.message.register(handle_answer, QuizStates.waiting_for_answer)

    # Start polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
