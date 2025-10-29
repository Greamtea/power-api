# --- Шаг 0: Импортируем наши "инструменты" ---
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
import json
import urllib.parse
from datetime import datetime

# --- ГЛОБАЛЬНЫЕ КОНСТАНТЫ ---
DTEK_URL = 'https://www.dtek-dnem.com.ua'
SHUTDOWNS_PAGE = DTEK_URL + '/ua/shutdowns'

# --- НОВЫЙ ЭЛЕМЕНТ: ПАСПОРТ БРАУЗЕРА (для обхода защиты) ---
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'


# --- Создаем "приложение" FastAPI ---
app = FastAPI()

# --- Создаем "точку входа" (Endpoint) ---
@app.get("/check")
async def check_power_outage(city: str, street: str, house: str):
    
    print(f"ПОЛУЧЕН ЗАПРОС: Город={city}, Улица={street}, Дом={house}")

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT}) # Прикрепляем "паспорт" ко всем запросам

    try:
        # --- Шаг 1: Получаем "Печать" (CSRF-токен) ---
        print("Шаг 1: Получаю 'печать' (CSRF-токен)...")

        response_main = session.get(SHUTDOWNS_PAGE)
        
        # Проверяем, что страница загрузилась
        if response_main.status_code != 200:
            print(f"ОШИБКА: Главная страница ДТЭК вернула статус {response_main.status_code}")
            return {"error": "Не удалось подключиться к сайту ДТЭК."}

        soup = BeautifulSoup(response_main.text, 'html.parser')
        token_tag = soup.find('meta', {'name': 'csrf-token'})
        
        if not token_tag:
            print("ОШИБКА: Не могу найти 'печать' (CSRF-токен). HTML мог измениться.")
            # Это сообщение ты видел. Теперь мы его обновим.
            return {"error": "Не могу получить токен с сайта ДТЭК."}

        fresh_token = token_tag['content']
        
        # --- Шаг 2: Отправляем "Заказ" (POST-запрос) ---
        print("\nШаг 2: Отправляю 'заказ' на получение графика...")

        ajax_url = DTEK_URL + '/ua/ajax'

        # Добавляем наш свежий токен и другие необходимые заголовки
        session.headers.update({
            'x-csrf-token': fresh_token,
            'x-requested-with': 'XMLHttpRequest',
            'Referer': SHUTDOWNS_PAGE
        })

        # Формируем "записку" (payload) с данными от пользователя
        # Мы используем urllib.parse.quote для правильного кодирования кириллицы
        payload = {
            'method': 'getHomeNum',
            'data[0][name]': 'city',
            'data[0][value]': city, 
            'data[1][name]': 'street',
            'data[1][value]': street, 
            'data[2][name]': 'updateFact',
            'data[2][value]': datetime.now().strftime("%d.%m.%Y %H:%M") # Отправляем текущее время
        }

        response_ajax = session.post(ajax_url, data=payload)

        # --- Шаг 3: Читаем и Фильтруем Ответ ---
        print("\nШаг 3: 'Заказ' выполнен! Фильтрую 'ответ'...")

        if response_ajax.status_code == 200:
            json_data = response_ajax.json()
            
            # НОВЫЙ ЭЛЕМЕНТ: Проверяем, вернулся ли ответ
            if json_data.get("result") != True:
                print("ОШИБКА: Сервер вернул 'result: false'. Вероятно, неверный адрес.")
                return {"status": "error", "message": "Адреса не знайдена або не обслуговується ДТЕК."}

            house_info = json_data.get("data", {}).get(house)
            
            if house_info:
                print(f"Найден дом '{house}'. Анализирую...")
                
                start_date = house_info.get("start_date")
                
                if start_date and start_date != "":
                    # Отключение ЕСТЬ!
                    end_date = house_info.get("end_date", "невідомо")
                    type_str = house_info.get("type", "Планове")
                    
                    print(f"!!! НАЙДЕНО ОТКЛЮЧЕНИЕ: {start_date} - {end_date}")
                    
                    # Возвращаем чистый JSON для приложения
                    return {
                        "status": "warning",
                        "message": f"{type_str} відключення",
                        "start_time": start_date,
                        "end_time": end_date
                    }
                else:
                    # Отключения НЕТ!
                    print("Отключений для этого дома нет.")
                    return {
                        "status": "ok",
                        "message": "Відключень не заплановано"
                    }
            else:
                # Дом не найден в ответе сервера
                print(f"ОШИБКА: Дом '{house}' не найден в ответе от ДТЭК.")
                return {"status": "error", "message": f"Дім '{house}' не знайдено за цією адресою."}
            
        else:
            print(f"ОШИБКА: Сервер ДТЭК вернул ошибку {response_ajax.status_code}")
            return {"status": "error", "message": "Сервер ДТЭК не отвечает."}

    except Exception as e:
        # Убираем лишнюю информацию, оставляя только сообщение для разработчика
        error_message = f"Внутренняя ошибка сервера: {type(e).__name__}"
        print(f"\nПроизошла непредвиденная ошибка: {error_message}")
        return {"status": "error", "message": "Внутренняя ошибка. Попробуйте позже."}

# ВАЖНО: Мы удаляем блок if __name__ == "__main__":, так как Vercel запускает приложение сам.
