# Telegram setup and live run

## 1. Create the bot

1. Open the verified `@BotFather` account in Telegram.
2. Send `/newbot` and choose the display name and username.
3. Copy the HTTP API token into `TELEGRAM_BOT_TOKEN` in the local `.env` file.
4. Do not put the token in source code, screenshots, commits, or chat messages.

## 2. Find the administrator chat ID

The administrator receives uncertain opportunities and can approve, reject, or request
another research cycle. Set `TELEGRAM_ADMIN_CHAT_ID` to the numeric Telegram ID of the
project owner. One direct method is:

1. Send any message to the new bot.
2. Locally open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.
3. Find `message.from.id` in the JSON response and copy only that number into `.env`.
4. Close the page; do not share its address because it contains the bot token.

Telegram account IDs are used as the application identity. Administrator callbacks also
enforce this configured ID, so another user cannot approve an opportunity by crafting a
callback.

## 3. Configure free LLM providers

Create a Groq API key and an OpenRouter API key in their official dashboards, then place
them in `.env`. At least one is required for live extraction. Both are recommended because
the model router automatically falls back when the first provider is rate limited or
unavailable.

Default model configuration:

```dotenv
GROQ_MODEL=openai/gpt-oss-20b
OPENROUTER_MODEL=openrouter/free
```

## 4. Launch and verify

```powershell
.venv\Scripts\opportunity-bot.exe
```

Expected flow:

1. Press **Start**.
2. Select the major, graduation year, and opportunity type with buttons.
3. Press **ابحث عن فرصة**.
4. A complete, official, matching opportunity is shown with **التقديم** and **حفظ**.
5. An uncertain result goes to the administrator; the student's workflow resumes after
   the administrator presses one of the review buttons.

The only typed Telegram command is the platform's initial `/start`. Every interaction
after entry is performed through inline buttons.

## 5. Troubleshooting

- `TELEGRAM_BOT_TOKEN is required`: the token is missing from `.env`.
- `GROQ_API_KEY or OPENROUTER_API_KEY is required`: configure at least one provider.
- No administrator review message: verify the numeric admin ID and message the bot once.
- A result is withheld: inspect structured logs for missing evidence, expiry, scope,
  eligibility, or a prompt-injection event. Withholding is expected safety behavior.
