import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime
import pytz # Потрібен для роботи з часовими зонами

# --- Створюємо "мозок" ---
app = FastAPI()

# --- Глобальні константи ---
DTEK_URL = 'https://www.dtek-dnem.com.ua'
SHUTDOWNS_PAGE = DTEK_URL + '/ua/shutdowns'
AJAX_URL = DTEK_URL + '/ua/ajax'

# "Паспорт" браузера
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'

# --- Словник для розшифровки класів ---
STATUS_MAP = {
    "cell-scheduled": "Світла немає",
    "cell-first-half": "Світла не буде перші 30 хв.",
    "cell-second-half": "Світла не буде другі 30 хв",
    "cell-non-scheduled": "Світло є"
}

# --- Головний метод (ендпоінт) ---
@app.get("/check")
async def check_power_outage(city: str = "", street: str = "", house: str = ""):
    
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    try:
        # --- Крок 1: Отримуємо "Печатку" (CSRF-токен) ---
        response_main = session.get(SHUTDOWNS_PAGE)
        if response_main.status_code != 200:
            return {"status": "error", "message": "Сайт ДТЕК не відповідає (головна)."}

        soup = BeautifulSoup(response_main.text, 'html.parser')
        token_tag = soup.find('meta', {'name': 'csrf-token'})
        
        if not token_tag:
            return {"status": "error", "message": "Не можу отримати токен (CSRF)."}

        fresh_token = token_tag['content']
        session.headers.update({
            'x-csrf-token': fresh_token,
            'x-requested-with': 'XMLHttpRequest',
            'Referer': SHUTDOWNS_PAGE
        })

        # --- Крок 2: Робимо AJAX-запит ---
        
        # Отримуємо поточний час для Києва
        tz_kyiv = pytz.timezone('Europe/Kiev')
        now = datetime.now(tz_kyiv)
        current_time_kyiv_str = now.strftime("%d.%m.%Y %H:%M")

        payload = {
            'method': 'getHomeNum',
            'data[0][name]': 'city',
            'data[0][value]': city,
            'data[1][name]': 'street',
            'data[1][value]': street,
            'data[2][name]': 'updateFact', 
            'data[2][value]': current_time_kyiv_str
        }
        
        response_ajax = session.post(AJAX_URL, data=payload)
        
        if response_ajax.status_code != 200:
            return {"status": "error", "message": f"Помилка AJAX-запиту ({response_ajax.status_code})"}

        json_data = response_ajax.json()

        # Якщо дім не вказано (запит списку вулиць/міст)
        if not house:
            list_data = json_data.get("data")
            if isinstance(list_data, list): return {"available_list": list_data}
            elif isinstance(list_data, dict): return {"available_list": list(list_data.keys())}
            else: return {"status": "error", "message": "Адресу не знайдено."}

        # ---
        # ✅ Крок 3: АНАЛІЗУЄМО HTML-ГРАФІК (Нова логіка)
        # ---
        
        html_content = json_data.get("content")
        if not html_content:
            # Це стара проблема - "data" є, а "content" немає
            house_info = json_data.get("data", {}).get(house)
            if not house_info:
                return {"status": "error", "message": "Дім не знайдено у 'data'."}
            
            # Обробляємо старий (аварійний) тип відповіді
            outage_type = house_info.get("type")
            if outage_type and outage_type != "":
                 return { "status": "warning", "message": "Світла немає (Аварійно)", "start_time": "", "end_time": "", "type": "Аварійне" }
            
            # Якщо і там нічого немає
            return {"status": "ok", "message": "Відключень не заплановано", "start_time": "", "end_time": "", "type": ""}

        # Парсимо HTML-графік
        soup_content = BeautifulSoup(html_content, 'html.parser')
        
        # Знаходимо АКТИВНИЙ (сьогоднішній) графік
        active_table = soup_content.find('div', {'class': 'discon-fact-table active'})
        if not active_table:
            return {"status": "error", "message": "Не можу знайти 'active' графік в HTML."}

        # Збираємо всі 24 години в один список
        hour_cells = []
        status_cells = []
        all_rows = active_table.find_all('tr')
        
        for row in all_rows:
            cols = row.find_all('td')
            if len(cols) == 2: # Це рядок даних (напр. <td>10-11</td> <td>cell-scheduled</td>)
                hour_cells.append(cols[0].text.strip()) # "10-11"
                status_cells.append(cols[1].get('class', [''])[0]) # "cell-scheduled"
        
        if len(hour_cells) != 24:
             return {"status": "error", "message": "Помилка парсингу HTML-таблиці (знайдено не 24 години)."}

        # --- Аналіз графіка ---
        current_hour_index = now.hour # Наприклад, 17
        current_minute = now.minute # Наприклад, 45

        # 1. Перевіряємо ПОТОЧНИЙ статус
        current_hour_text = hour_cells[current_hour_index] # "17-18"
        current_status_class = status_cells[current_hour_index] # "cell-non-scheduled"

        start_time_str = current_hour_text.split('-')[0] + ":00" # "17:00"
        end_time_str = current_hour_text.split('-')[1] + ":00" # "18:00"

        # Перевірка, чи світло вимкнене ЗАРАЗ
        is_off_now = False
        if current_status_class == 'cell-scheduled':
            is_off_now = True
        elif current_status_class == 'cell-first-half' and current_minute < 30:
            is_off_now = True
            end_time_str = start_time_str[:2] + ":30" # 17:30
        elif current_status_class == 'cell-second-half' and current_minute >= 30:
            is_off_now = True
            start_time_str = start_time_str[:2] + ":30" # 17:30

        if is_off_now:
            return {
                "status": "warning", # "warning" = світла немає ЗАРАЗ
                "message": f"Світла немає (до {end_time_str})",
                "start_time": start_time_str,
                "end_time": end_time_str,
                "type": "Планове"
            }

        # 2. Якщо світло ЗАРАЗ є, шукаємо НАСТУПНЕ відключення
        # Починаємо пошук з ПОТОЧНОЇ години (на випадок, якщо відключення о 17:30, а зараз 17:00)
        for i in range(current_hour_index, 24):
            hour_text = hour_cells[i] # "17-18", "18-19", ...
            status_class = status_cells[i]
            
            # Пропускаємо поточну годину, якщо вона 'cell-non-scheduled'
            if i == current_hour_index and status_class == 'cell-non-scheduled':
                continue
                
            # Перевіряємо майбутні години
            if status_class == 'cell-scheduled':
                start = hour_text.split('-')[0] + ":00"
                end = hour_text.split('-')[1] + ":00"
                return {"status": "ok", "message": f"Наступне відключення о {start}", "start_time": start, "end_time": end, "type": "Планове"}
            
            elif status_class == 'cell-first-half':
                start = hour_text.split('-')[0] + ":00"
                end = hour_text.split('-')[0] + ":30"
                # Якщо зараз 17:00, а відключення 17:00-17:30, це ПОТОЧНЕ
                if i == current_hour_index: continue # (це вже оброблено вище)
                return {"status": "ok", "message": f"Наступне відключення о {start}", "start_time": start, "end_time": end, "type": "Планове"}

            elif status_class == 'cell-second-half':
                start = hour_text.split('-')[0] + ":30"
                end = hour_text.split('-')[1] + ":00"
                # Якщо зараз 17:40, а відключення 17:30-18:00, це ПОТОЧНЕ
                if i == current_hour_index: continue # (це вже оброблено вище)
                return {"status": "ok", "message": f"Наступне відключення о {start}", "start_time": start, "end_time": end, "type": "Планове"}

        # 3. Якщо сьогодні більше нічого немає
        return {
            "status": "ok",
            "message": "Відключень не заплановано",
            "start_time": "",
            "end_time": "",
            "type": ""
        }

    except Exception as e:
        return {"status": "error", "message": f"Внутрішня помилка API: {str(e)}"}
