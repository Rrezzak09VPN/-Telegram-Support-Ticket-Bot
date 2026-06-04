[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram%20Bot%20API-7.0+-blue.svg)](https://core.telegram.org/bots/api)

# 🎫 Telegram Support Ticket Bot

Простой и удобный Telegram-бот для организации технической поддержки через **тикеты** и **Telegram Topics (форум-темы)**.

## 🚀 Что умеет бот

✅ Создание тикетов пользователями через личные сообщения боту

✅ Автоматическое создание отдельной темы (Topic) в Telegram-группе для каждого тикета

✅ Переписка между пользователем и администраторами без раскрытия контактов

✅ Отправка текста, фото, документов, видео, голосовых сообщений, кружков и стикеров

✅ Закрытие тикетов пользователем или администратором

✅ Система банов и разбанов пользователей

✅ Ограничение спама (Rate Limit)

✅ Ограничение размера файлов

✅ Автоматическая ротация логов

✅ Автоматические резервные копии базы данных

✅ Автозапуск через systemd

---

## 🖥 Установка

```bash
sudo bash <(curl -sL https://raw.githubusercontent.com/Rrezzak09VPN/-Telegram-Support-Ticket-Bot/main/install.sh)
```

Установщик автоматически:

* Установит Python и зависимости
* Создаст виртуальное окружение
* Настроит systemd
* Создаст базу данных SQLite
* Настроит резервное копирование
* Настроит logrotate
* Запустит бота

## ⚙️ Как работает бот

1. Пользователь пишет боту в личные сообщения.
2. Нажимает **«Создать тикет»**.
3. Бот создаёт новую тему (Topic) в указанной Telegram-группе.
4. Все сообщения пользователя автоматически попадают в эту тему.
5. Администраторы отвечают прямо в теме группы.
6. Ответ автоматически пересылается пользователю в личные сообщения.
7. После решения вопроса тикет можно закрыть.

---

## 📋 Что потребуется

### 1️⃣ BOT_TOKEN

Токен Telegram-бота.

Получить можно у:

👉 @BotFather

Команда:

`/newbot`

После создания бота BotFather выдаст токен вида:

```text
123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Вставить в:

```env
BOT_TOKEN="ВАШ_ТОКЕН"
```

---

### 2️⃣ GROUP_ID

ID Telegram-группы, где будут создаваться тикеты.

⚠️ Группа должна быть:

* Супергруппой
* С включёнными Topics (Темами)
* Бот должен быть администратором

Пример:

```env
GROUP_ID=-1001234567890
```

---

### 3️⃣ ADMIN_IDS

Telegram ID администраторов.

Узнать свой ID можно через:

👉 @userinfobot

Пример:

```env
ADMIN_IDS=123456789
```

Несколько администраторов:

```env
ADMIN_IDS=123456789,987654321
```

---

### 4️⃣ PROJECT_NAME

Название проекта, отображаемое в сообщениях бота.

Пример:

```env
PROJECT_NAME="🚀 Support Bot 🌐"
```

---

## 📝 Пример config.env

```env
BOT_TOKEN="123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
GROUP_ID=-1001234567890
ADMIN_IDS=123456789
PROJECT_NAME="🚀 Support Bot 🌐"

MAX_FILE_SIZE=20971520
MAX_OPEN_TICKETS=1
MAX_MESSAGES_PER_MINUTE=10

DATA_DIR="/opt/support-bot/data"

MAX_LOG_SIZE_MB=50
LOG_BACKUP_COUNT=5
```

---

---

## 🔧 Управление сервисом

Статус:

```bash
systemctl status support-bot
```

Перезапуск:

```bash
systemctl restart support-bot
```

Остановка:

```bash
systemctl stop support-bot
```

Просмотр логов:

```bash
journalctl -u support-bot -f
```

---

## 💾 Резервные копии

База данных автоматически архивируется каждый день.

Ручной запуск:

```bash
/opt/support-bot/backup.sh
```

---

## 📂 Структура данных

```text
/opt/support-bot/
├── bot.py
├── config.env
├── backup.sh
├── uninstall.sh
├── data/
│   ├── tickets.db
│   └── bot.log
└── backups/
```

---

## ⚠️ Важно

Перед запуском обязательно:

✅ Включить Topics (Темы) в Telegram-группе

✅ Выдать боту права администратора

✅ Указать правильный GROUP_ID

✅ Указать свой ADMIN_ID

После этого бот полностью готов к работе 🎉
