import asyncio
import os
import json
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import OpenAI
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# ===================== НАСТРОЙКИ =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Для Render.com

# Проверка наличия всех переменных
if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, ASSISTANT_ID, BITRIX_WEBHOOK]):
    raise ValueError("⚠️ Не все переменные окружения установлены!")

# Инициализация
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Хранилище thread_id для каждого пользователя
user_threads = {}

class ConversationState(StatesGroup):
    chatting = State()

# ===================== ФУНКЦИИ BITRIX24 =====================

def create_bitrix_lead(name, phone, email=None, comments=None, telegram_id=None):
    """
    Создает лида в Bitrix24
    """
    url = f"{BITRIX_WEBHOOK}crm.lead.add.json"
    
    # Формируем комментарий с Telegram ID
    full_comments = comments or "Лид создан через Telegram бота"
    if telegram_id:
        full_comments += f"\nTelegram ID: {telegram_id}"
    
    data = {
        "fields": {
            "TITLE": f"GPT: {name}",
            "NAME": name,
            "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}],
            "STATUS_ID": "NEW",
            "SOURCE_ID": "OTHER",
            "COMMENTS": full_comments
        }
    }
    
    # Добавляем email, если указан
    if email:
        data["fields"]["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
    
    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        
        if result.get("result"):
            lead_id = result["result"]
            print(f"✅ Лид создан в Bitrix24: ID {lead_id}")
            return {
                "success": True,
                "lead_id": lead_id,
                "message": f"Лид успешно создан в CRM. ID: {lead_id}"
            }
        else:
            error_msg = result.get("error_description", "Неизвестная ошибка")
            print(f"❌ Ошибка создания лида: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "message": "Не удалось создать лида в CRM"
            }
    except Exception as e:
        print(f"❌ Ошибка при обращении к Bitrix24: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Ошибка соединения с CRM"
        }

# ===================== ОБРАБОТКА FUNCTION CALLING =====================

def handle_function_call(function_name, arguments, user_id):
    """
    Обрабатывает вызов функции от Assistant
    """
    if function_name == "create_bitrix24_lead":
        # Парсим аргументы
        args = json.loads(arguments)
        
        name = args.get("name")
        phone = args.get("phone")
        email = args.get("email")
        comments = args.get("comments")
        
        # Вызываем функцию создания лида
        result = create_bitrix_lead(
            name=name,
            phone=phone,
            email=email,
            comments=comments,
            telegram_id=user_id
        )
        
        return json.dumps(result, ensure_ascii=False)
    
    return json.dumps({"success": False, "message": "Неизвестная функция"})

# ===================== TELEGRAM HANDLERS =====================

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Пользователь"
    
    # Создаем новый thread для пользователя
    thread = openai_client.beta.threads.create()
    user_threads[user_id] = thread.id
    
    await message.answer(
        f"👋 Добро пожаловать в НСК, {username}!\n\n"
        "Я помогу вам узнать о системах GPS/ГЛОНАСС мониторинга транспорта "
        "и контроле расхода топлива.\n\n"
        "Что вас интересует?\n"
        "🚛 Мониторинг транспорта\n"
        "⛽ Контроль топлива\n"
        "💰 Стоимость системы\n"
        "🔄 Переход от другой системы\n"
        "👤 Связь с менеджером"
    )
    
    await state.set_state(ConversationState.chatting)

@dp.message(ConversationState.chatting)
async def message_handler(message: types.Message):
    user_id = message.from_user.id
    user_message = message.text
    
    # Получаем или создаем thread
    if user_id not in user_threads:
        thread = openai_client.beta.threads.create()
        user_threads[user_id] = thread.id
    
    thread_id = user_threads[user_id]
    
    try:
        # Отправляем сообщение в thread
        openai_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )
        
        # Показываем "печатает..."
        await bot.send_chat_action(message.chat.id, "typing")
        
        # Запускаем Assistant
        run = openai_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )
        
        # Ожидаем завершения с обработкой функций
        max_iterations = 60  # Максимум 30 секунд (60 * 0.5)
        iteration = 0
        
        while run.status in ["queued", "in_progress", "requires_action"] and iteration < max_iterations:
            await asyncio.sleep(0.5)
            iteration += 1
            
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )
            
            # Если Assistant требует вызова функции
            if run.status == "requires_action":
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                tool_outputs = []
                
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    arguments = tool_call.function.arguments
                    
                    print(f"🔧 Вызов функции: {function_name}")
                    print(f"📋 Аргументы: {arguments}")
                    
                    # Обрабатываем вызов функции
                    output = handle_function_call(function_name, arguments, user_id)
                    
                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": output
                    })
                
                # Отправляем результаты функций обратно в Assistant
                run = openai_client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs
                )
        
        # Проверяем статус завершения
        if run.status == "completed":
            # Получаем ответ
            messages = openai_client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )
            
            assistant_response = messages.data.content.text.value
            
            # Отправляем ответ пользователю (макс 4096 символов)
            if len(assistant_response) > 4096:
                # Разбиваем на части
                for i in range(0, len(assistant_response), 4096):
                    await message.answer(
                        assistant_response[i:i+4096],
                        parse_mode="Markdown"
                    )
            else:
                await message.answer(assistant_response, parse_mode="Markdown")
                
        elif run.status == "failed":
            await message.answer(
                "😔 Извините, произошла ошибка обработки. Попробуйте еще раз или "
                "свяжитесь с нами: +7 (342) 225-29-58"
            )
        elif run.status == "expired":
            await message.answer(
                "⏱️ Время ожидания истекло. Попробуйте еще раз с более коротким сообщением."
            )
        
    except Exception as e:
        print(f"❌ Ошибка в message_handler: {e}")
        import traceback
        traceback.print_exc()
        await message.answer(
            "😔 Извините, произошла техническая ошибка. "
            "Свяжитесь с нами напрямую: sale@nsc-navi.ru или +7 (342) 225-29-58"
        )

@dp.message(F.text == "/reset")
async def reset_handler(message: types.Message):
    user_id = message.from_user.id
    
    # Создаем новый thread
    thread = openai_client.beta.threads.create()
    user_threads[user_id] = thread.id
    
    await message.answer("🔄 Диалог сброшен. Начнем сначала!")

@dp.message(F.text == "/help")
async def help_handler(message: types.Message):
    await message.answer(
        "📖 **Доступные команды:**\n\n"
        "/start - Начать диалог\n"
        "/reset - Сбросить историю диалога\n"
        "/help - Показать эту справку\n\n"
        "**Контакты:**\n"
        "📧 Email: sale@nsc-navi.ru\n"
        "📞 Телефон: +7 (342) 225-29-58\n"
        "🌐 Сайт: https://nsc-navi.ru",
        parse_mode="Markdown"
    )

# ===================== ЗАПУСК БОТА =====================

async def main():
    print("🤖 Telegram бот НСК запущен!")
    print(f"📋 Assistant ID: {ASSISTANT_ID}")
    print(f"🔗 Bitrix24 Webhook: {BITRIX_WEBHOOK[:50]}...")
    
    # Для локального запуска используем polling
    print("📡 Режим работы: Polling (долгий опрос)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
