# 💅 Nail Bot

Телеграм-бот для записи к мастеру маникюра.

## Возможности
- 📅 Запись на процедуры
- 💰 Прайс-лист
- 📸 Портфолио работ
- ⭐ Отзывы с рейтингом
- 📋 Управление записями
- ⚙️ Админ-панель

## Установка на сервер

```bash
git clone https://github.com/ваш_username/nail-bot.git
cd nail-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python3 bot.py
