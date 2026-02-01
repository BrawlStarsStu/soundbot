import glob
import os
import yt_dlp # Добавь этот импорт в самое начало файла
import requests
import asyncio
import sqlite3
import logging
from soundcloud import SoundCloud  # Вместо SoundcloudAPI
# В этой библиотеке Track импортировать не нужно, она возвращает объекты данных
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = '8457225521:AAHOJvW6yUO0JKFcNvnD-fgdLZho1BLk9nA'
DB_FILE = 'music_bot.db'

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
sc = SoundCloud()  # Клиент SoundCloud

# Глобальный кэш поиска (храним в памяти результаты поиска для пагинации)
# Структура: {user_id: [список объектов треков]}
SEARCH_CACHE = {}

# --- РАБОТА С БАЗОЙ ДАННЫХ ---

def init_db():
    """Создает таблицы, если их нет"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Таблица истории
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            user_id INTEGER,
            query TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица избранного (здесь храним file_id от Telegram, чтобы не качать заново)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            track_title TEXT,
            track_artist TEXT,
            sc_url TEXT,
            tg_file_id TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_history(user_id, query):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Удаляем старые записи, если их больше 5
    cursor.execute("INSERT INTO history (user_id, query) VALUES (?, ?)", (user_id, query))
    conn.commit()
    # Оставляем только последние 5
    cursor.execute("""
        DELETE FROM history WHERE rowid IN (
            SELECT rowid FROM history 
            WHERE user_id = ? 
            ORDER BY timestamp DESC LIMIT -1 OFFSET 5
        )
    """, (user_id,))
    conn.commit()
    conn.close()

def get_history(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT query FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5", (user_id,))
    data = cursor.fetchall()
    conn.close()
    return [row[0] for row in data]

def add_favorite(user_id, track, file_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Проверка на дубликаты
    cursor.execute("SELECT id FROM favorites WHERE user_id = ? AND sc_url = ?", (user_id, track.permalink_url))
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO favorites (user_id, track_title, track_artist, sc_url, tg_file_id)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, track.title, track.artist, track.permalink_url, file_id))
    conn.commit()
    conn.close()

def get_favorites(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, track_artist, track_title, tg_file_id FROM favorites WHERE user_id = ?", (user_id,))
    data = cursor.fetchall()
    conn.close()
    return data # List of tuples

def get_cached_file_id(sc_url):
    """Ищет, загружали ли мы уже этот трек (глобально, любым юзером)"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT tg_file_id FROM favorites WHERE sc_url = ? LIMIT 1", (sc_url,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# --- КЛАВИАТУРЫ ---

def get_main_menu():
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📜 История"), KeyboardButton(text="⭐️ Избранное")]
    ], resize_keyboard=True)
    return kb

def get_pagination_keyboard(user_id, page, total_tracks):
    builder = InlineKeyboardBuilder()
    
    # Генерируем кнопки треков (5 штук на страницу)
    start = page * 5
    end = start + 5
    tracks = SEARCH_CACHE.get(user_id, [])[start:end]
    
    for idx, track in enumerate(tracks):
        # Важно: callback содержит глобальный индекс трека в списке
        global_index = start + idx
        # Обрезаем название, чтобы кнопка не была гигантской
        btn_text = f"{track.artist} - {track.title}"[:40]
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"play:{global_index}"))
    
    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
    if end < total_tracks:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page:{page+1}"))
    
    builder.row(*nav_buttons)
    return builder.as_markup()

def get_player_keyboard(track_idx_or_id, artist_name, is_db_id=False):
    """
    track_idx_or_id: Индекс в кэше поиска ИЛИ ID в базе данных
    is_db_id: Если True, то track_idx_or_id - это ID из таблицы favorites
    """
    builder = InlineKeyboardBuilder()
    
    # Если играем из поиска, даем возможность добавить в избранное
    if not is_db_id:
        builder.add(InlineKeyboardButton(text="❤️ В избранное", callback_data=f"add_fav:{track_idx_or_id}"))
    
    # Кнопка поиска по артисту
    builder.add(InlineKeyboardButton(text="👤 Этот исполнитель", callback_data=f"search_artist:{artist_name[:20]}"))
    
    return builder.as_markup()

# --- ХЕНДЛЕРЫ (ОБРАБОТЧИКИ) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Введи название песни или исполнителя, и я найду музыку в SoundCloud.", reply_markup=get_main_menu())

@dp.message(F.text == "📜 История")
async def show_history(message: types.Message):
    history = get_history(message.from_user.id)
    if not history:
        await message.answer("История пуста.")
        return
    
    builder = InlineKeyboardBuilder()
    for query in history:
        builder.row(InlineKeyboardButton(text=query, callback_data=f"hist_search:{query}"))
    
    await message.answer("Твои последние запросы:", reply_markup=builder.as_markup())

@dp.message(F.text == "⭐️ Избранное")
async def show_favorites(message: types.Message):
    favs = get_favorites(message.from_user.id) # [(id, artist, title, file_id), ...]
    if not favs:
        await message.answer("В избранном пусто.")
        return
    
    builder = InlineKeyboardBuilder()
    for fav in favs:
        db_id, artist, title, _ = fav
        builder.row(InlineKeyboardButton(text=f"{artist} - {title}"[:40], callback_data=f"play_fav:{db_id}"))
    
    await message.answer("Твои сохраненные треки:", reply_markup=builder.as_markup())

# Обработка поиска из Истории
@dp.callback_query(F.data.startswith("hist_search:"))
async def history_search_handler(callback: types.CallbackQuery):
    query = callback.data.split(":", 1)[1]
    await callback.message.delete() # Удаляем меню истории
    # Вызываем функцию поиска (имитируем ввод текста)
    await perform_search(callback.message, query, callback.from_user.id)

# Обработка кнопки "Исполнитель"
@dp.callback_query(F.data.startswith("search_artist:"))
async def artist_search_handler(callback: types.CallbackQuery):
    artist = callback.data.split(":", 1)[1]
    await perform_search(callback.message, artist, callback.from_user.id)
    await callback.answer()

# Основная функция поиска (используется и для текста, и для кнопок)
async def perform_search(message: types.Message, query: str, user_id: int):
    status_msg = await message.answer(f"🔎 Ищу в SoundCloud: {query}...")
    add_history(user_id, query)
    
    try:
        # В soundcloud-v2 метод называется search_tracks
        # Мы используем asyncio.to_thread, чтобы сетевой запрос не вешал бота
        search_results = await asyncio.to_thread(sc.search_tracks, query)
        
        # Превращаем генератор в список и берем первые 30 штук
        tracks = []
        for i, track in enumerate(search_results):
            if i >= 30: break
            
            # ИСПРАВЛЕНИЕ ТУТ: Обращаемся через точку .username
            try:
                track.artist = track.user.username 
            except AttributeError:
                track.artist = "Unknown Artist"
                
            tracks.append(track)
        
        if not tracks:
            await status_msg.edit_text("Ничего не найдено 😔")
            return
            
        SEARCH_CACHE[user_id] = tracks
        await status_msg.delete()
        await message.answer(f"Результаты по запросу '{query}':", 
                             reply_markup=get_pagination_keyboard(user_id, 0, len(tracks)))
        
    except Exception as e:
        logging.error(f"Search error: {e}")
        await status_msg.edit_text(f"Ошибка при поиске: {e}")
@dp.message(F.text)
async def text_search_handler(message: types.Message):
    if message.text in ["📜 История", "⭐️ Избранное"]: return # Игнорируем кнопки меню
    await perform_search(message, message.text, message.from_user.id)

# Обработка пагинации (Стрелочки)
@dp.callback_query(F.data.startswith("page:"))
async def pagination_handler(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    if user_id not in SEARCH_CACHE:
        await callback.answer("Результаты устарели, повтори поиск.")
        return
        
    total = len(SEARCH_CACHE[user_id])
    try:
        await callback.message.edit_reply_markup(reply_markup=get_pagination_keyboard(user_id, page, total))
    except Exception:
        pass # Если клавиатура не изменилась
    await callback.answer()

# Обработка нажатия на трек из ПОИСКА
@dp.callback_query(F.data.startswith("play:"))
async def play_search_track(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    if user_id not in SEARCH_CACHE:
        await callback.answer("Ошибка кэша. Повтори поиск.")
        return
        
    track = SEARCH_CACHE[user_id][idx]
    await send_audio_track(callback.message, track, idx_for_fav=idx)
    await callback.answer()

# Обработка нажатия на трек из ИЗБРАННОГО
@dp.callback_query(F.data.startswith("play_fav:"))
async def play_fav_track(callback: types.CallbackQuery):
    db_id = int(callback.data.split(":")[1])
    
    # Достаем file_id из базы
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT tg_file_id, track_artist, track_title FROM favorites WHERE id = ?", (db_id,))
    res = cursor.fetchone()
    conn.close()
    
    if res:
        file_id, artist, title = res
        # Отправляем сразу по file_id (мгновенно)
        await callback.message.answer_audio(
            audio=file_id, 
            caption=f"🎧 {artist} - {title}",
            reply_markup=get_player_keyboard(db_id, artist, is_db_id=True)
        )
    else:
        await callback.answer("Трек не найден в базе.")

# Логика скачивания и отправки
async def send_audio_track(message, track, idx_for_fav):
    t_title = getattr(track, 'title', 'Unknown Track')
    wait_msg = await message.answer(f"⬇️ Загружаю: {t_title}...")
    
    # Генерируем уникальное имя файла, чтобы запросы не пересекались
    filename = f"track_{getattr(track, 'id', 'temp')}.mp3"
    
    try:
        # Настройки yt-dlp для SoundCloud
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': filename, # Куда сохранять
            'quiet': True,
            'noprogress': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }

        # Скачиваем через yt-dlp по прямой ссылке на страницу трека
        url = track.permalink_url
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])

        # Проверяем, появился ли файл (иногда yt-dlp добавляет .mp3 к расширению)
        if not os.path.exists(filename) and os.path.exists(filename + ".mp3"):
            filename += ".mp3"

        # Отправляем в Telegram
        audio_file = FSInputFile(filename)
        artist_name = getattr(track.user, 'username', 'Unknown')
        
        sent_msg = await message.answer_audio(
            audio=audio_file,
            title=t_title,
            performer=artist_name,
            reply_markup=get_player_keyboard(idx_for_fav, artist_name)
        )
        
        # Кэшируем ID для избранного
        track.temp_file_id = sent_msg.audio.file_id
        await wait_msg.delete()

    except Exception as e:
        logging.error(f"YT-DLP Error: {e}")
        await message.answer(f"⚠️ Ошибка загрузки: {str(e)[:100]}")
    finally:
        # Чистим за собой
        if os.path.exists(filename):
            try: os.remove(filename)
            except: pass
# Обработка добавления в избранное
@dp.callback_query(F.data.startswith("add_fav:"))
async def add_fav_handler(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    if user_id not in SEARCH_CACHE:
        await callback.answer("Сессия поиска устарела.")
        return
        
    track = SEARCH_CACHE[user_id][idx]
    
    # Нам нужен file_id. Если мы только что скачали трек, он есть в track.temp_file_id (см. выше)
    # Если мы не сохранили его там, придется снова качать или брать из сообщения (сложно)
    # Упрощение: предполагаем, что если юзер нажал кнопку, трек есть в сообщении выше.
    
    # Самый надежный способ: взять file_id из аудио сообщения, к которому прикреплена кнопка
    file_id = callback.message.audio.file_id
    
    add_favorite(user_id, track, file_id)
    await callback.answer("✅ Добавлено в избранное!")

# --- ЗАПУСК ---

async def main():
    init_db()
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
