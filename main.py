from dotenv import load_dotenv
import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums.parse_mode import ParseMode
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
        f"Ресторан: PROBKA\n\nБлюдо: {dish.get('name')}\n\nПожалуйста, напиши его рецепт.",
        reply_markup=MAIN_KEYBOARD,
    )


async def stream_evaluation_to_message(
    status_message: types.Message,
    dish_name: str,
    official_recipe: str,
    user_recipe: str,
    price: str,
    weight: str,
) -> str:
    """
    Stream model response token-by-token and edit the Telegram message as text arrives.
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

    buffer: list[str] = []
    last_sent_text = ""
    last_update = time.monotonic()
    min_interval = 0.4
    min_chars = 20

    try:
        stream = await openai_client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=True,
        )
        async for event in stream:
            delta = event.choices[0].delta.content or ""
            if not delta:
                continue
            buffer.append(delta)
            current_text = "".join(buffer).strip()
            now = time.monotonic()
            if len(current_text) - len(last_sent_text) >= min_chars or now - last_update >= min_interval:
                if current_text and current_text != last_sent_text:
                    await status_message.edit_text(current_text)
                    last_sent_text = current_text
                    last_update = now
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenAI evaluation failed: %s", exc)
        return "Не удалось получить оценку от модели.\nПопробуй, пожалуйста, другое блюдо чуть позже."

    final_text = "".join(buffer).strip()
    if final_text and final_text != last_sent_text:
        await status_message.edit_text(final_text, parse_mode=ParseMode.MARKDOWN)
    logger.debug("OpenAI evaluation response: %s", final_text)
    return final_text


async def handle_answer(message: types.Message, state: FSMContext) -> None:
    """
    Handle user's recipe answer.
    """
    status_message = await message.answer("Проверяю ответ...")
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

    await stream_evaluation_to_message(
        status_message,
        dish.get("name", "Неизвестное блюдо"),
        official_recipe,
        user_recipe,
        dish.get("price", "Неизвестная цена"),
        dish.get("weight", "Неизвестный вес"),
    )

    image_url = dish.get("image_url")

    # Send image separately after streaming response (if available)
    if image_url:
        logger.debug(
            "Sending evaluation with image for dish '%s' to user %s",
            dish.get("name"),
            user_id,
        )
        await message.answer_photo(photo=image_url, reply_markup=MAIN_KEYBOARD)
    else:
        logger.debug(
            "Sending evaluation without image for dish '%s' to user %s",
            dish.get("name"),
            user_id,
        )

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
