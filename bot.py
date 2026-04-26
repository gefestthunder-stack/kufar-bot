import asyncio
import aiohttp
import json
import re
import os
from telegram import Bot
from telegram.constants import ParseMode

# ===== НАСТРОЙКИ =====
BOT_TOKEN = "8713454535:AAGbJN1dyog2guW8vP5L2Mu-JRaAEuJfmhg"           # от @BotFather
CHAT_ID = "@gefestthunder"
GOOGLE_VISION_KEY = "ВАШ_GOOGLE_KEY"  # бесплатно 1000/месяц
CHECK_INTERVAL = 1800
MAX_PRICE = 950
SEEN_FILE = "seen_ads.json"

CITIES = {
    "Брест": 7,
    "Кобрин": 268,
    "Жабинка": 277
}

SEARCH_QUERIES = [
    "iPhone 13 Pro",
    "iPhone 13 Pro Max",
    "Huawei Pura 70",
    "Huawei Pura 70 Pro"
]

BAD_WORDS = [
    "битый", "трещин", "скол", "после ремонт", "восстановлен",
    "ремонтировал", "разбит", "дефект", "не работает", "на запчасти",
    "замена стекл", "замена экран", "замена корпус"
]

GOOD_WORDS = [
    "не битый", "идеал", "отличное состояние", "как новый",
    "хорошее состояние", "без дефектов", "состояние 10/10",
    "состояние 9/10", "не ремонтировался"
]

# ===== SEEN ADS =====
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ===== ИЗВЛЕЧЕНИЕ ДАННЫХ ИЗ ТЕКСТА =====
def extract_battery(text):
    if not text:
        return None
    patterns = [
        r'акб[:\s]*(\d{2,3})\s*%',
        r'батаре[яи][:\s]*(\d{2,3})\s*%',
        r'battery[:\s]*(\d{2,3})\s*%',
        r'ёмкост[ьи][:\s]*(\d{2,3})\s*%',
        r'(\d{2,3})\s*%\s*акб',
        r'(\d{2,3})\s*%\s*батар',
        r'акб\s*(\d{2,3})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = int(match.group(1))
            if 60 <= val <= 100:
                return val
    return None

def extract_storage(text):
    if not text:
        return None
    match = re.search(r'(\d+)\s*(гб|gb)', text.lower())
    if match:
        return match.group(1) + " ГБ"
    return "Не указано"

def extract_color(text):
    if not text:
        return "Не указано"
    colors = [
        "черный", "чёрный", "белый", "синий", "голубой", "золотой",
        "серебристый", "серый", "зеленый", "зелёный", "красный",
        "фиолетовый", "graphite", "silver", "gold", "blue", "black",
        "white", "purple", "green", "alpine green", "sierra blue",
        "midnight", "starlight", "titanium"
    ]
    text_lower = text.lower()
    for color in colors:
        if color in text_lower:
            return color.capitalize()
    return "Не указано"

def check_description(text):
    """
    Возвращает: "good", "bad", "unclear"
    """
    if not text:
        return "unclear"
    text_lower = text.lower()
    
    for word in BAD_WORDS:
        if word in text_lower:
            return "bad"
    
    for word in GOOD_WORDS:
        if word in text_lower:
            return "good"
    
    return "unclear"

# ===== GOOGLE VISION АНАЛИЗ ФОТО =====
async def check_photo_vision(session, image_url, api_key):
    """
    Отправляет фото в Google Vision.
    Ищет метки связанные с повреждениями.
    Возвращает: "good", "bad", "unclear"
    """
    try:
        # Скачиваем картинку и конвертируем в base64
        async with session.get(image_url) as resp:
            if resp.status != 200:
                return "unclear"
            img_data = await resp.read()
            import base64
            img_b64 = base64.b64encode(img_data).decode("utf-8")

        vision_url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        payload = {
            "requests": [{
                "image": {"content": img_b64},
                "features": [
                    {"type": "LABEL_DETECTION", "maxResults": 20},
                    {"type": "SAFE_SEARCH_DETECTION"}
                ]
            }]
        }

        async with session.post(vision_url, json=payload) as resp:
            if resp.status != 200:
                return "unclear"
            data = await resp.json()

        labels = data["responses"][0].get("labelAnnotations", [])
        label_names = [l["description"].lower() for l in labels]

        # Метки которые говорят о повреждениях
        bad_labels = ["crack", "broken", "damaged", "scratch", "fracture", "shattered"]
        for label in label_names:
            for bad in bad_labels:
                if bad in label:
                    return "bad"

        return "good"

    except Exception:
        return "unclear"

# ===== ЗАПРОС К КУФАРУ =====
async def fetch_kufar(session, query, city_id):
    url = "https://api.kufar.by/search-api/v2/search/rendered-paginated"
    params = {
        "lang": "ru",
        "query": query,
        "cat": "1611",       # категория: мобильные телефоны
        "rgn": city_id,
        "cur": "BYR",
        "prc_min": 0,
        "prc_max": MAX_PRICE * 100,  # Куфар хранит цены в копейках
        "size": 30,
        "sort": "lst.d"      # сортировка: сначала новые
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36"
    }
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"Ошибка запроса: {e}")
    return None

# ===== ОБРАБОТКА ОБЪЯВЛЕНИЯ =====
async def process_ad(session, ad, city_name, api_key):
    ad_id = str(ad.get("ad_id", ""))
    
    title = ad.get("subject", "")
    description = ad.get("body", "")
    full_text = f"{title} {description}"
    
    # Цена
    price_raw = ad.get("price_byn", 0)
    price = round(price_raw / 100) if price_raw else 0
    if price > MAX_PRICE or price == 0:
        return None, ad_id
    
    # Проверка описания
    desc_status = check_description(full_text)
    if desc_status == "bad":
        return None, ad_id
    
    # Данные
    battery = extract_battery(full_text)
    storage = extract_storage(full_text)
    color = extract_color(full_text)
    
    # Фото
    photos = ad.get("images", [])
    photo_url = None
    photo_status = "unclear"
    
    if photos:
        # Куфар отдаёт фото в разных размерах
        first_photo = photos[0]
        # Пробуем получить URL фото
        if isinstance(first_photo, dict):
            photo_url = first_photo.get("url", None)
            if not photo_url:
                # Иногда структура другая
                ids = first_photo.get("id", "")
                if ids:
                    photo_url = f"https://rms.kufar.by/v1/gallery/{ids[:2]}/{ids[2:4]}/{ids}.jpg"
        
        if photo_url and api_key and api_key != "ВАШ_GOOGLE_KEY":
            photo_status = await check_photo_vision(session, photo_url, api_key)
            if photo_status == "bad":
                return None, ad_id
    
    # Формируем статус состояния
    if desc_status == "good" and photo_status == "good":
        condition = "✅ Не битый (текст + фото)"
    elif desc_status == "good":
        condition = "✅ Не битый (по описанию)"
    elif photo_status == "good":
        condition = "✅ Не битый (по фото)"
    else:
        condition = "⚠️ Состояние не указано — проверь сам"
    
    link = f"https://www.kufar.by/item/{ad_id}"
    
    result = {
        "ad_id": ad_id,
        "title": title,
        "storage": storage,
        "battery": battery,
        "color": color,
        "price": price,
        "city": city_name,
        "condition": condition,
        "photo_url": photo_url,
        "link": link
    }
    return result, ad_id

# ===== ОТПРАВКА В TELEGRAM =====
async def send_ad(bot, ad_data):
    battery_str = f"{ad_data['battery']}%" if ad_data['battery'] else "Не указано"
    
    text = (
        f"📱 *{ad_data['title']}*\n"
        f"💾 Память: {ad_data['storage']}\n"
        f"🔋 АКБ: {battery_str}\n"
        f"🎨 Цвет: {ad_data['color']}\n"
        f"💵 Цена: {ad_data['price']} BYN\n"
        f"📍 Город: {ad_data['city']}\n"
        f"🛡 Состояние: {ad_data['condition']}\n"
        f"🔗 [Открыть объявление]({ad_data['link']})"
    )
    
    try:
        if ad_data["photo_url"]:
            await bot.send_photo(
                chat_id=CHAT_ID,
                photo=ad_data["photo_url"],
                caption=text,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        # Если фото не загрузилось — шлём без фото
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e2:
            print(f"Ошибка отправки текста: {e2}")

# ===== ГЛАВНЫЙ ЦИКЛ =====
async def main():
    bot = Bot(token=BOT_TOKEN)
    seen = load_seen()
    
    await bot.send_message(
        chat_id=CHAT_ID,
        text="🤖 Бот запущен! Мониторю Куфар каждые 30 минут.\n"
             "Города: Брест, Кобрин, Жабинка\n"
             "Модели: iPhone 13 Pro/Pro Max, Huawei Pura 70/Pro\n"
             f"Макс. цена: {MAX_PRICE} BYN"
    )
    
    async with aiohttp.ClientSession() as session:
        while True:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Проверяю Куфар...")
            new_count = 0
            
            for city_name, city_id in CITIES.items():
                for query in SEARCH_QUERIES:
                    await asyncio.sleep(2)  # пауза между запросами
                    
                    data = await fetch_kufar(session, query, city_id)
                    if not data:
                        continue
                    
                    ads = data.get("ads", [])
                    
                    for ad in ads:
                        ad_id = str(ad.get("ad_id", ""))
                        if ad_id in seen:
                            continue
                        
                        result, ad_id = await process_ad(
                            session, ad, city_name, GOOGLE_VISION_KEY
                        )
                        
                        seen.add(ad_id)
                        
                        if result:
                            await send_ad(bot, result)
                            new_count += 1
                            await asyncio.sleep(1)
            
            save_seen(seen)
            print(f"Готово. Найдено новых: {new_count}. Жду 30 минут...")
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
