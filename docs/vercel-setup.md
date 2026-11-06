# Как запустить QR-бота на Vercel

Займёт около 10 минут. Банковская карта не нужна. Токены никому не присылай, в том числе Claude: ты сам вписываешь их в секреты GitHub (шаг 3).

## 1. Регистрация на Vercel

1. Открой https://vercel.com/signup.
2. Выбери тариф **Hobby** (бесплатный, для личных проектов).
3. Нажми **Continue with GitHub** и войди под `NGoodma`.
4. Если Vercel предложит импортировать репозиторий (Import Git Repository), **пропусти**. Бота выкладывает GitHub Actions. Если подключить репозиторий ещё и здесь, каждый push будет выкладываться дважды.

## 2. Токен для GitHub Actions

1. Справа вверху аватар → **Account Settings** → **Tokens**. Прямая ссылка: https://vercel.com/account/tokens
2. **Create Token**:
   - Name: `github-actions`
   - Scope: свой аккаунт (Hobby)
   - Expiration: `No Expiration`. Если выберешь срок, по его окончании выкладка перестанет работать, и токен придётся создать заново.
3. Скопируй токен: Vercel показывает его только один раз.

## 3. Секреты в репозитории `NGoodma/QR`

https://github.com/NGoodma/QR/settings/secrets/actions → **New repository secret**:

| Имя | Что вставить |
|---|---|
| `VERCEL_TOKEN` | токен из шага 2 |
| `TELEGRAM_BOT_TOKEN` | токен QR-бота от @BotFather (тот же, что был в `.env`) |

## 4. Останови старый бот

Если старая версия где-то запущена (`python bot.py` на Mac), останови её. Иначе она будет конфликтовать с webhook.

## 5. Выкладка

Вкладка **Actions** вверху репозитория (не Settings) → **test-and-deploy** → **Run workflow**. Каждый следующий push в `main` будет выкладывать бота сам.

Скрипт `scripts/deploy.py` создаст проект на Vercel, положит туда токен бота, отключит экран входа Vercel (иначе Telegram не достучится до бота), выложит код на адрес `qr-ngoodma.vercel.app` и подключит webhook.

## Для следующих ботов

Токен Vercel подходит для всех ботов. Для нового бота нужны свой репозиторий по этому образцу, свой бот в BotFather и те же два секрета.
