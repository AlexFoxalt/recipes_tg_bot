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
from aiogram.enums import ParseMode
from openai import AsyncOpenAI

from prompts import SYSTEM_PROMPT, USER_PROMPT

load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
RECIPES_PATH = BASE_DIR / "recipes.json"

with RECIPES_PATH.open("r", encoding="utf-8") as f:
    RECIPES = json.load(f)

if not isinstance(RECIPES, list) or not RECIPES:
    raise RuntimeError("recipes.json must contain a non-empty list of recipes")

logger.info("Loaded %d recipes from %s", len(RECIPES), RECIPES_PATH)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

TRY_NEXT_BUTTON_TEXT = "Новое блюдо"

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
    logger.info(
        "User %s (%s) started the bot",
        message.from_user.id if message.from_user else "unknown",
        message.from_user.username if message.from_user else "unknown",
    )
    await message.answer(
        "Нажимай кнопку снизу, чтобы начать тест.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_try_next(message: types.Message, state: FSMContext) -> None:
    """
    Handle pressing the 'Try next' button:
    - pick a random dish
    - ask user to write its recipe
    """
    user_id = message.from_user.id if message.from_user else "unknown"
    dish = random.choice(RECIPES)
    logger.info(
        "User %s requested new dish: '%s'",
        user_id,
        dish.get("name"),
    )

    # Store current dish in state for future extensions (e.g. checking answer)
    await state.update_data(current_dish_name=dish.get("name"))
    await state.set_state(QuizStates.waiting_for_answer)

    await message.answer(
        f"Блюдо: {dish.get('name')}\n\nПожалуйста, напиши его рецепт.",
        reply_markup=MAIN_KEYBOARD,
    )


async def evaluate_answer_with_model(
    dish_name: str, official_recipe: str, user_recipe: str, price: str, weight: str
) -> str:
    """
    Use OpenAI model to compare user's recipe with the official one and rate it.
    """
    if openai_client is None:
        logger.error("OpenAI client is not initialized")
        return "Evaluation service is temporarily unavailable. Please try another dish later."

    logger.info("Sending evaluation request to OpenAI for dish '%s'", dish_name)
    logger.debug(
        "User recipe preview: %s",
        (user_recipe[:120] + "...") if len(user_recipe) > 120 else user_recipe,
    )

    prompt = USER_PROMPT.format(
        dish_name=dish_name,
        official_recipe=official_recipe,
        user_recipe=user_recipe,
        price=price,
        weight=weight,
    )

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenAI evaluation failed: %s", exc)
        return "Не удалось получить оценку от модели.\nПопробуй, пожалуйста, другое блюдо чуть позже."

    content = (response.choices[0].message.content or "").strip()
    logger.debug("OpenAI evaluation response: %s", content)
    return content


async def handle_answer(message: types.Message, state: FSMContext) -> None:
    """
    Handle user's recipe answer.
    """
    await message.answer(
        "Проверяю ответ...",
        reply_markup=MAIN_KEYBOARD,
    )
    user_id = message.from_user.id if message.from_user else "unknown"
    data = await state.get_data()
    dish_name = data.get("current_dish_name")

    dish = None
    if dish_name:
        dish = next((d for d in RECIPES if d.get("name") == dish_name), None)

    if not dish:
        logger.warning(
            "Dish not found in state for user %s (state dish_name=%r)",
            user_id,
            dish_name,
        )
        # Fallback if we, for some reason, lost the dish in state
        await message.answer(
            "В этот раз я не смогл найти официальный рецепт, но вы можете попробовать другое блюдо.",
            reply_markup=MAIN_KEYBOARD,
        )
        await state.clear()
        return

    official_recipe = dish.get("recipe") or "Описание рецепта отсутствует."
    user_recipe = message.text or ""

    logger.info(
        "Evaluating answer from user %s for dish '%s'",
        user_id,
        dish.get("name", "Unknown dish"),
    )

    evaluation = await evaluate_answer_with_model(
        dish.get("name", "Неизвестное блюдо"),
        official_recipe,
        user_recipe,
        dish.get("price", "Неизвестная цена"),
        dish.get("weight", "Неизвестный вес"),
    )

    image_url = dish.get("image_url")

    # Send only model response and image (if available)
    if image_url:
        logger.debug(
            "Sending evaluation with image for dish '%s' to user %s",
            dish.get("name"),
            user_id,
        )
        await message.answer_photo(
            photo=image_url, caption=evaluation, reply_markup=MAIN_KEYBOARD, parse_mode=ParseMode.MARKDOWN
        )
    else:
        logger.debug(
            "Sending evaluation without image for dish '%s' to user %s",
            dish.get("name"),
            user_id,
        )
        await message.answer(evaluation, reply_markup=MAIN_KEYBOARD)

    # Clear state so user can start a new round by pressing the button again
    await state.clear()
    logger.info("Cleared state for user %s after evaluation", user_id)


async def main() -> None:
    """
    Entry point for the Telegram bot.
    """
    # Basic logging setup
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting Telegram recipes quiz bot")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in the environment")
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment")
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is not set in the environment")
        raise RuntimeError("OPENAI_API_KEY is not set in the environment")

    global openai_client
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI client initialized successfully")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    logger.info("Aiogram Dispatcher and Bot initialized")

    # Register handlers
    dp.message.register(start_handler, CommandStart())
    dp.message.register(handle_try_next, F.text == TRY_NEXT_BUTTON_TEXT)
    dp.message.register(handle_answer, QuizStates.waiting_for_answer)
    logger.info("Handlers registered; starting polling")

    # Start polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
