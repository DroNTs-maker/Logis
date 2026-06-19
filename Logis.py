import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.filters import Command
import sqlite3

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = "8828259702:AAFIjrdUZYs4czgF1ftrPR6HmqWabCg1RMM"
DEEPSEEK_API_KEY = os.getenv("sk-61cd3a0e7fe94d6facf2891d15529c44")
LOGISTICS_CHAT_ID = os.getenv("1181111312")  # для уведомлений о дедлайнах
BOT_USERNAME = os.getenv("Logis", "Logisticorganizerbot")  # без @

TARGET_USERNAME = "t3ang1e"  # для шуток
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DB_FILE = "orders.db"

# Возможные статусы заявки
STATUSES = [
    "Новая",
    "В работе",
    "Машина подана",
    "Загрузилась",
    "Выгрузилась",
    "Завершена"
]

# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_number TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            chat_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            load_date TEXT,
            load_time TEXT,           -- "09:00" или "к 9:00"
            shipper_name TEXT,
            shipper_address TEXT,
            shipper_contact TEXT,
            consignee_name TEXT,
            consignee_address TEXT,
            consignee_contact TEXT,
            cargo_weight TEXT,
            cargo_volume TEXT,
            cargo_description TEXT,
            order_numbers TEXT,
            notes TEXT,
            truck_number TEXT,        -- госномер машины, заполняется позже
            status TEXT DEFAULT 'Новая',
            raw_text TEXT
        )
    ''')
    # Индексы для быстрого поиска
    c.execute('CREATE INDEX IF NOT EXISTS idx_track ON orders(track_number)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_status ON orders(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_date ON orders(user_id, load_date)')
    conn.commit()
    conn.close()

def generate_track_number() -> str:
    """Короткий уникальный трек-номер (8 символов)."""
    return uuid.uuid4().hex[:8].upper()

def save_order(user_id: int, username: str, full_name: str, chat_id: int,
               parsed_data: dict, raw_text: str) -> str:
    """Сохраняет заказ в БД и возвращает трек-номер."""
    track = generate_track_number()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO orders (
            track_number, user_id, username, full_name, chat_id,
            load_date, load_time,
            shipper_name, shipper_address, shipper_contact,
            consignee_name, consignee_address, consignee_contact,
            cargo_weight, cargo_volume, cargo_description,
            order_numbers, notes, status, raw_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        track, user_id, username, full_name, chat_id,
        parsed_data.get("load_date"),
        parsed_data.get("load_time"),
        parsed_data.get("shipper_name"),
        parsed_data.get("shipper_address"),
        parsed_data.get("shipper_contact"),
        parsed_data.get("consignee_name"),
        parsed_data.get("consignee_address"),
        parsed_data.get("consignee_contact"),
        parsed_data.get("cargo_weight"),
        parsed_data.get("cargo_volume"),
        parsed_data.get("cargo_description"),
        parsed_data.get("order_numbers"),
        parsed_data.get("notes"),
        "Новая",
        raw_text
    ))
    conn.commit()
    conn.close()
    return track

def check_duplicate(user_id: int, load_date: str, shipper_address: str,
                    consignee_address: str) -> Optional[str]:
    """Проверяет, есть ли уже заявка с такими же параметрами от этого пользователя сегодня.
    Возвращает трек-номер найденного дубликата или None."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT track_number FROM orders
        WHERE user_id = ? AND load_date = ? AND shipper_address = ? AND consignee_address = ?
        AND date(created_at) = date('now')
        LIMIT 1
    ''', (user_id, load_date, shipper_address, consignee_address))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_order_by_track(track_number: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE track_number = ?', (track_number,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    # Превращаем в словарь с именами колонок
    columns = [desc[0] for desc in c.description]  # не очень красиво, но работает
    return dict(zip(columns, row))

def update_order_status(track_number: str, new_status: str) -> bool:
    if new_status not in STATUSES:
        return False
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE track_number = ?
    ''', (new_status, track_number))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok

def update_truck_number(track_number: str, truck_number: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE orders SET truck_number = ? WHERE track_number = ?', (truck_number, track_number))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok

def search_orders(query: str) -> List[Dict[str, Any]]:
    """Ищет заказы по всем текстовым полям."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    like_query = f"%{query}%"
    c.execute('''
        SELECT track_number, load_date, shipper_name, consignee_name, status, created_at
        FROM orders
        WHERE shipper_address LIKE ? OR consignee_address LIKE ?
           OR shipper_name LIKE ? OR consignee_name LIKE ?
           OR order_numbers LIKE ? OR notes LIKE ?
           OR truck_number LIKE ?
        ORDER BY created_at DESC
        LIMIT 10
    ''', (like_query, like_query, like_query, like_query, like_query, like_query, like_query))
    rows = c.fetchall()
    conn.close()
    results = []
    for row in rows:
        results.append({
            "track_number": row[0],
            "load_date": row[1],
            "shipper_name": row[2],
            "consignee_name": row[3],
            "status": row[4],
            "created_at": row[5]
        })
    return results

def count_today_orders_by_username(username: str) -> int:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM orders WHERE username = ? AND date(created_at) = ?", (username, today))
    count = c.fetchone()[0]
    conn.close()
    return count

# ---------- DEEPSEEK ----------
async def ask_deepseek(prompt: str, system_message: str = "Ты ассистент-логист.") -> str:
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 2000
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(DEEPSEEK_API_URL, headers=headers, json=payload) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

async def generate_joke() -> str:
    prompt = (
        "Придумай одну короткую забавную фразу в русском разговорном стиле, "
        "которая начинается с обращения 'Эльвек' и содержит смысл: "
        "'Эльвек, остановись, ты сегодня слишком много заказов делаешь, "
        "ты в ударе, мощный, не остановить'. "
        "Сделай каждый раз по-новому, с иронией и дружеским подколом. "
        "Только фраза, без дополнительных объяснений."
    )
    return await ask_deepseek(prompt, "Ты веселый помощник в чате логистов.")

# ---------- ПАРСИНГ С УЛУЧШЕННЫМ ПРОМПТОМ ----------
async def extract_orders_from_text(raw_text: str) -> List[Dict[str, Any]]:
    system = (
        "Ты парсишь запросы на перевозку. Возвращай ТОЛЬКО JSON-массив объектов без markdown-обёртки.\n"
        "Разбей текст на отдельные заказы (каждый начинается с 'Прошу найти машину' или аналогично).\n"
        "Для каждого заказа извлеки следующие поля (если не указано – ставь null):\n"
        " - load_date (дата погрузки в формате ДД.ММ.ГГГГ или текстом, например 'завтра 10.05')\n"
        " - load_time (время погрузки, если указано явно, например 'к 9:00' или '09:00'; иначе null)\n"
        " - shipper_name (название отправителя, например ООО ФОРУМ ЭЛЕКТРО)\n"
        " - shipper_address (адрес загрузки)\n"
        " - shipper_contact (контакты на загрузке: телефоны, имена)\n"
        " - consignee_name (название получателя)\n"
        " - consignee_address (адрес поставки)\n"
        " - consignee_contact (контакты получателя)\n"
        " - cargo_weight (вес груза, строка)\n"
        " - cargo_volume (объём груза, строка)\n"
        " - cargo_description (описание груза, количество мест, габариты)\n"
        " - order_numbers (номера ЗП, ГК, счетов, перечисли через запятую)\n"
        " - notes (любые дополнительные требования, тип машины)\n\n"
        "Примеры:\n\n"
        "Текст 1:\n"
        "Прошу найти машину на завтра 10.05 для отгрузки ООО ФОРУМ ЭЛЕКТРО\n"
        "Габариты: Вес 3,22 кг, объём 0,01160 м3\n"
        "Адрес отгрузки: Тверь, технопарк ДКС, д.4, тел. +74822777980 доб.2569\n"
        "Адрес поставки: Москва, Лосевская ул., д.3, Мелихов Андрей, +7-925-565-19-76\n\n"
        "JSON: [{\"load_date\":\"завтра 10.05\",\"load_time\":null,\"shipper_name\":\"ООО ФОРУМ ЭЛЕКТРО\","
        "\"shipper_address\":\"Тверь, технопарк ДКС, д.4\",\"shipper_contact\":\"+74822777980 доб.2569\","
        "\"consignee_name\":\"Мелихов Андрей Анатольевич\","
        "\"consignee_address\":\"Москва, Лосевская ул., вблизи д.3\","
        "\"consignee_contact\":\"+7-925-565-19-76\",\"cargo_weight\":\"3,22 кг\",\"cargo_volume\":\"0,01160 м3\","
        "\"cargo_description\":null,\"order_numbers\":null,\"notes\":null}]\n\n"
        "Текст 2:\n"
        "Прошу найти машину на 11.06 для отгрузки ООО Литекс\n"
        "Адрес отгрузки: Долгопрудный, Промышленный проезд, 8В, Жилин Сергей, 89586390177\n"
        "Габариты: 31 кг, 4 коробки 40*30*45, 1 коробка 40*30*45\n"
        "Адрес доставки: Алтайский край, г Бийск, ул Лесная 25, Кобзев Дмитрий, +79913699616\n\n"
        "JSON: [{\"load_date\":\"11.06\",\"load_time\":null,\"shipper_name\":\"ООО Литекс\","
        "\"shipper_address\":\"Долгопрудный, Промышленный проезд, 8В\","
        "\"shipper_contact\":\"Жилин Сергей, 89586390177\","
        "\"consignee_name\":\"Кобзев Дмитрий Алексеевич\","
        "\"consignee_address\":\"Алтайский край, г Бийск, ул Лесная 25\","
        "\"consignee_contact\":\"+79913699616\",\"cargo_weight\":\"31 кг\",\"cargo_volume\":null,"
        "\"cargo_description\":\"4 коробки 40*30*45, 1 коробка 40*30*45\","
        "\"order_numbers\":null,\"notes\":null}]"
    )
    response = await ask_deepseek(f"Текст сообщения:\n{raw_text}", system)
    response_clean = re.sub(r'```(?:json)?\s*|```', '', response).strip()
    try:
        orders = json.loads(response_clean)
        if isinstance(orders, dict):
            orders = [orders]
        return orders
    except json.JSONDecodeError:
        logging.error(f"Ошибка парсинга JSON: {response_clean}")
        return []

def missing_fields(order: dict) -> List[str]:
    required = {
        "load_date": "дата погрузки",
        "shipper_address": "адрес загрузки",
        "consignee_address": "адрес доставки"
    }
    return [desc for field, desc in required.items() if not order.get(field)]

# ---------- FSM ----------
class OrderState(StatesGroup):
    waiting_for_missing = State()

# ---------- БОТ ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Фильтр: обрабатываем только сообщения с ключевой фразой или упоминанием
def is_relevant_message(message: Message) -> bool:
    text = message.text or ""
    # Прямое упоминание бота
    if f"@{BOT_USERNAME}" in text.lower():
        return True
    # Ключевые фразы начала заявки
    if re.search(r"прошу найти машину|нужна фура|найти транспорт", text, re.IGNORECASE):
        return True
    return False

# ---------- КОМАНДЫ ПО УПОМИНАНИЮ ----------
def extract_mention_command(text: str) -> Optional[str]:
    """Извлекает команду после @botname."""
    pattern = rf"@{re.escape(BOT_USERNAME)}\s+(?P<cmd>.+)"
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group("cmd").strip() if m else None

@dp.message(F.text & F.func(is_relevant_message))
async def handle_text(message: Message, state: FSMContext):
    """Главный обработчик входящих сообщений."""
    text = message.text
    # Сначала проверим, не команда ли это через упоминание
    if f"@{BOT_USERNAME}" in text.lower():
        cmd = extract_mention_command(text)
        if cmd:
            await process_mention_command(message, cmd, state)
            return
        # Если упоминание без команды – игнорируем или подскажем
        await message.answer("Используйте команду после упоминания, например: статус заявки АВС123")
        return

    # Если это заявка ("прошу найти машину...")
    if re.search(r"прошу найти машину|нужна фура|найти транспорт", text, re.IGNORECASE):
        await handle_order_creation(message, state)

async def handle_order_creation(message: Message, state: FSMContext):
    raw_text = message.text
    orders = await extract_orders_from_text(raw_text)
    if not orders:
        await message.answer("Не удалось распознать заявку. Попробуйте переформулировать.")
        return

    # Обрабатываем только первую заявку для уточнения (если несколько — сообщим)
    order = orders[0]
    if len(orders) > 1:
        await message.answer("Найдено несколько заявок. Для корректного оформления присылайте по одной.")

    # Проверка дублей
    user = message.from_user
    duplicate_track = check_duplicate(
        user.id,
        order.get("load_date", ""),
        order.get("shipper_address", ""),
        order.get("consignee_address", "")
    )
    if duplicate_track:
        await message.answer(
            f"⚠️ Похоже, вы уже создавали такую заявку сегодня (трек {duplicate_track}). "
            "Если это новая – уточните адрес или дату."
        )
        return

    # Проверяем полноту
    missing = missing_fields(order)
    if missing:
        await state.set_state(OrderState.waiting_for_missing)
        await state.update_data(current_order=order, raw_text=raw_text)
        await message.answer(
            "Не хватает данных:\n• " + "\n• ".join(missing) +
            "\nПожалуйста, отправьте недостающую информацию."
        )
        return

    # Сохраняем
    track = save_order(user.id, user.username or "", user.full_name, message.chat.id, order, raw_text)

    # Шутка для Эльвека
    joke = None
    if user.username and user.username.lower() == TARGET_USERNAME.lower():
        if count_today_orders_by_username(user.username) > 2:
            joke = await generate_joke()

    reply = f"✅ Заявка принята. Трек-номер: {track}\n"
    reply += f"📅 Дата: {order.get('load_date')}\n"
    reply += f"🏭 Откуда: {order.get('shipper_address')}\n"
    reply += f"📍 Куда: {order.get('consignee_address')}\n"
    reply += f"📦 Статус: Новая"
    if joke:
        reply += f"\n\n😂 {joke}"
    await message.answer(reply)

@dp.message(F.text, OrderState.waiting_for_missing)
async def process_missing_info(message: Message, state: FSMContext):
    data = await state.get_data()
    current_order = data["current_order"]
    raw_text = data["raw_text"]
    additional = message.text
    combined = f"{raw_text}\n---\n{additional}"
    orders = await extract_orders_from_text(combined)
    if not orders:
        await message.answer("Не удалось извлечь данные. Попробуйте ещё раз.")
        return
    updated = orders[0]
    missing = missing_fields(updated)
    if missing:
        await state.update_data(current_order=updated, raw_text=combined)
        await message.answer("Всё ещё не хватает:\n• " + "\n• ".join(missing))
        return

    user = message.from_user
    track = save_order(user.id, user.username or "", user.full_name, message.chat.id, updated, combined)
    reply = f"✅ Заявка сохранена. Трек-номер: {track}\n"
    reply += f"📅 Дата: {updated.get('load_date')}\n"
    reply += f"🏭 Откуда: {updated.get('shipper_address')}\n"
    reply += f"📍 Куда: {updated.get('consignee_address')}"
    await message.answer(reply)
    await state.clear()

# ---------- ОБРАБОТКА КОМАНД ПО УПОМИНАНИЮ ----------
async def process_mention_command(message: Message, command: str, state: FSMContext):
    """Разбирает команду после @botname."""
    command = command.lower().strip()
    user = message.from_user

    # Статус заявки
    if command.startswith("статус заявки"):
        # Формат: статус заявки <трек>
        parts = command.split()
        if len(parts) >= 3:
            track = parts[2].upper()
        else:
            await message.answer("Укажите трек-номер: статус заявки АВС123")
            return
        order = get_order_by_track(track)
        if not order:
            await message.answer(f"Заявка с треком {track} не найдена.")
            return
        await message.answer(
            f"📋 Заявка {track}\n"
            f"Статус: {order['status']}\n"
            f"Дата погрузки: {order['load_date']}\n"
            f"От: {order['shipper_name']} ({order['shipper_address']})\n"
            f"До: {order['consignee_name']} ({order['consignee_address']})"
        )
        return

    # Смена статуса: статус заявки <трек> <новый статус>
    # или "сменить статус заявки ..."
    if any(phrase in command for phrase in ["сменить статус", "изменить статус", "статус заявки"]):
        # Пытаемся извлечь трек и новый статус
        words = command.replace("сменить статус заявки", "").replace("изменить статус заявки", "").replace("статус заявки", "").strip().split()
        if len(words) >= 2:
            track = words[0].upper()
            new_status = " ".join(words[1:]).capitalize()
            # Проверяем, есть ли такой статус
            if new_status not in STATUSES:
                await message.answer(f"Неизвестный статус. Доступны: {', '.join(STATUSES)}")
                return
            if update_order_status(track, new_status):
                await message.answer(f"✅ Статус заявки {track} изменён на «{new_status}».")
            else:
                await message.answer("Не удалось обновить статус. Проверьте трек-номер.")
        else:
            await message.answer("Формат: статус заявки АВС123 В работе")
        return

    # Поиск
    if command.startswith("поиск"):
        query = command.replace("поиск", "").strip()
        if not query:
            await message.answer("Введите поисковый запрос: поиск Москва Тверь")
            return
        results = search_orders(query)
        if not results:
            await message.answer("Ничего не найдено.")
        else:
            lines = ["🔍 Результаты поиска:"]
            for r in results:
                lines.append(
                    f"• Трек: {r['track_number']} | {r['load_date']} | "
                    f"{r['shipper_name']} → {r['consignee_name']} | {r['status']}"
                )
            await message.answer("\n".join(lines[:15]))  # не более 15 строк
        return

    # Статистика
    if "статистика" in command:
        today = date.today().isoformat()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM orders WHERE date(created_at) = ?", (today,))
        total = c.fetchone()[0]
        c.execute("SELECT username, full_name, COUNT(*) FROM orders WHERE date(created_at) = ? GROUP BY username ORDER BY COUNT(*) DESC", (today,))
        rows = c.fetchall()
        conn.close()
        stats = f"📊 За {today}: всего заказов {total}\nПо менеджерам:\n"
        for uname, fname, cnt in rows:
            name = fname if fname else uname
            stats += f"• {name} (@{uname}): {cnt}\n"
        await message.answer(stats)
        return

    # Аналогичные перевозки (по последнему заказу пользователя)
    if "найти аналогичные" in command:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (user.id,))
        last = c.fetchone()
        if not last:
            await message.answer("У вас ещё нет заказов.")
            conn.close()
            return
        # Индексы колонок могут меняться, используем поиск по адресам отдельным запросом
        c.execute('''SELECT track_number, shipper_name, consignee_name, load_date
                     FROM orders WHERE id != ? AND (shipper_address LIKE ? OR consignee_address LIKE ?)
                     LIMIT 5''',
                  (last[0], f"%{last[8]}%", f"%{last[11]}%"))
        similar = c.fetchall()
        conn.close()
        if not similar:
            await message.answer("Похожих заявок не найдено.")
        else:
            reply = "🔎 Похожие заявки:\n"
            for t, sn, cn, ld in similar:
                reply += f"• {t} | {ld} | {sn} → {cn}\n"
            await message.answer(reply)
        return

    # Неизвестная команда
    await message.answer(
        "Доступные команды:\n"
        f"• @{BOT_USERNAME} статус заявки <трек>\n"
        f"• @{BOT_USERNAME} статус заявки <трек> <статус>\n"
        f"• @{BOT_USERNAME} поиск <ключевые слова>\n"
        f"• @{BOT_USERNAME} статистика\n"
        f"• @{BOT_USERNAME} найти аналогичные перевозки"
    )

# ---------- ДЕДЛАЙН КОНТРОЛЬ (фоновая задача) ----------
async def deadline_control():
    """Каждые 60 секунд проверяет заявки и, если за час до load_time статус не 'Машина подана', шлёт напоминание."""
    if not LOGISTICS_CHAT_ID:
        logging.warning("LOGISTICS_CHAT_ID не задан — контроль дедлайнов отключён.")
        return
    while True:
        try:
            now = datetime.now()
            # Берём все заявки в статусах "Новая" или "В работе", у которых load_date = сегодня
            # и load_time задан, и разница load_time - now < 60 минут, но load_time ещё не прошло
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            today_str = now.strftime("%d.%m.%Y")  # нужно сопоставлять с тем, что хранится в load_date
            # Но load_date может быть в разных форматах, поэтому берем все и фильтруем в коде
            c.execute("SELECT track_number, load_date, load_time, status FROM orders WHERE status IN ('Новая','В работе')")
            rows = c.fetchall()
            conn.close()

            for track, load_date, load_time, status in rows:
                if not load_time:
                    continue
                # Парсим load_time ("09:00", "к 9:00")
                time_match = re.search(r"(\d{1,2}):(\d{2})", load_time)
                if not time_match:
                    continue
                hour, minute = int(time_match.group(1)), int(time_match.group(2))
                # Парсим load_date – ожидаем ДД.ММ.ГГГГ или "завтра 10.05"
                # Упростим: если есть "завтра", берём завтрашнюю дату; иначе пробуем распарсить.
                target_date = None
                if "завтра" in load_date.lower():
                    target_date = (now + timedelta(days=1)).date()
                else:
                    # ищем паттерн ДД.ММ
                    date_match = re.search(r"(\d{2})\.(\d{2})", load_date)
                    if date_match:
                        day, month = int(date_match.group(1)), int(date_match.group(2))
                        year = now.year
                        # предполагаем, что дата в текущем году (или будущем)
                        try:
                            target_date = date(year, month, day)
                        except:
                            continue
                if not target_date:
                    continue
                # Сравниваем дату погрузки с сегодня
                if target_date != now.date():
                    continue
                # Время погрузки как datetime
                load_datetime = datetime.combine(now.date(), datetime.min.time().replace(hour=hour, minute=minute))
                # Если до погрузки осталось меньше часа и статус не "Машина подана"
                if (load_datetime - now) <= timedelta(minutes=60) and (load_datetime - now) > timedelta(0):
                    await bot.send_message(
                        LOGISTICS_CHAT_ID,
                        f"⏰ Напоминание: заявка {track} должна грузиться в {load_time} ({load_date}), но статус всё ещё «{status}». "
                        "Машина не подана!"
                    )
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Ошибка в deadline_control: {e}")
            await asyncio.sleep(60)

@dp.startup()
async def on_startup():
    logging.basicConfig(level=logging.INFO)
    init_db()
    asyncio.create_task(deadline_control())

# ---------- ЗАПУСК ----------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
