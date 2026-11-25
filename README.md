# Free Finder UA 🇺🇦

Бот ищет на **OLX.ua** в разделе «Віддам безкоштовно» (и дешёвое до N грн в выбранных категориях) вещи, которые можно перепродать на Etsy/eBay.

Каждые 20 минут (в :13, :33, :53) робот на GitHub Actions:
1. читает раздел, пока не дойдёт до уже просмотренных объявлений (до 25 страниц);
2. отбрасывает животных, детские вещи, мебель и т.п. (категории OLX + фильтр слов);
3. отдаёт оставшееся ИИ (Gemini) — оценка 0–10, цена Etsy/eBay, винтаж ли, ссылка на проданные лоты;
4. находки с оценкой от порога присылает в Telegram-канал и сохраняет в дашборд.

Дашборд: https://olx-ua-free-finder.web.app — вход только для двух Google-аккаунтов (см. `firestore.rules`).

## Секреты репозитория (Settings → Secrets and variables → Actions)

| Секрет | Что это |
|---|---|
| `TELEGRAM_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | id канала (`-100…`), узнаётся запуском в режиме `chat-id` |
| `GEMINI_API_KEY` | ключ Google AI Studio (лучше в отдельном проекте — свой дневной лимит) |
| `FIREBASE_SERVICE_ACCOUNT` | JSON сервисного аккаунта Firebase-проекта `olx-ua-free-finder` |

Ручной запуск: Actions → Free stuff finder UA → Run workflow (режимы run / test / chat-id / dry-run) или кнопка «Проверить сейчас» в дашборде.
