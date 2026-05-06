import os
import json
import base64
import logging
import anthropic
import gspread
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config from environment variables ────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID   = os.environ["GOOGLE_SPREADSHEET_ID"]
ALLOWED_USER_ID  = int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", "0"))  # 0 = allow anyone
CURRENCY         = os.environ.get("CURRENCY", "IDR")

# ── Google Sheets setup ───────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet("Expenses")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Expenses", rows=1000, cols=10)
        ws.append_row(["Date", "Time", "Amount", "Currency", "Category", "Description", "Notes", "Source"])
        # Style header row
        ws.format("A1:H1", {
            "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.4},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
    return ws

# ── Claude AI parsing ─────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SYSTEM_PROMPT = f"""You are an expense parsing assistant. Extract expense information and return ONLY valid JSON.

Categories to use (pick the best fit):
- Food & Drinks       (restaurants, cafes, groceries, snacks)
- Transport           (ride-hail, fuel, parking, toll, public transit)
- Shopping            (clothes, electronics, household items)
- Health & Beauty     (pharmacy, doctor, salon, gym)
- Entertainment       (movies, events, subscriptions, games)
- Utilities & Bills   (electricity, internet, phone, rent)
- Travel              (hotels, flights, travel expenses)
- Education           (books, courses, tuition)
- Business            (work-related expenses, client meals)
- Other               (anything that doesn't fit above)

Return JSON with these fields:
{{
  "amount": <number or null if unclear>,
  "currency": "{CURRENCY}",
  "category": "<one of the categories above>",
  "description": "<short clean description, max 40 chars>",
  "notes": "<any extra context, or empty string>",
  "confidence": "high|medium|low"
}}

If the message is NOT an expense (e.g. a greeting or question), return:
{{"not_expense": true}}
"""

def clean_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()

def parse_text_expense(text: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Parse this expense: {text}"}],
    )
    raw = clean_json(response.content[0].text)
    logger.info(f"Claude raw response: {raw}")
    return json.loads(raw)

def parse_receipt_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": b64},
                },
                {"type": "text", "text": "This is a receipt. Extract the expense information."},
            ],
        }],
    )
    raw = clean_json(response.content[0].text)
    logger.info(f"Claude raw response: {raw}")
    return json.loads(raw)

# ── Append to Google Sheets ───────────────────────────────────────────────────
def save_to_sheet(parsed: dict, source: str = "text"):
    ws = get_sheet()
    now = datetime.now()
    row = [
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        parsed.get("amount", ""),
        parsed.get("currency", CURRENCY),
        parsed.get("category", "Other"),
        parsed.get("description", ""),
        parsed.get("notes", ""),
        source,
    ]
    ws.append_row(row)

# ── Format reply message ──────────────────────────────────────────────────────
def format_reply(parsed: dict) -> str:
    amount = parsed.get("amount")
    amount_str = f"{CURRENCY} {int(amount):,}" if amount else "Amount unclear"
    cat   = parsed.get("category", "Other")
    desc  = parsed.get("description", "")
    notes = parsed.get("notes", "")
    conf  = parsed.get("confidence", "high")

    emoji_map = {
        "Food & Drinks": "🍜",
        "Transport": "🚗",
        "Shopping": "🛍️",
        "Health & Beauty": "💊",
        "Entertainment": "🎬",
        "Utilities & Bills": "💡",
        "Travel": "✈️",
        "Education": "📚",
        "Business": "💼",
        "Other": "📌",
    }
    cat_emoji = emoji_map.get(cat, "📌")

    msg = f"✅ *Expense saved!*\n\n"
    msg += f"💰 *{amount_str}*\n"
    msg += f"{cat_emoji} {cat}\n"
    msg += f"📝 {desc}"
    if notes:
        msg += f"\n💬 _{notes}_"
    if conf == "low":
        msg += "\n\n⚠️ _Low confidence — please double-check the entry._"
    return msg

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    text = update.message.text.strip()

    # Commands
    if text.lower() in ["/start", "/help"]:
        await update.message.reply_text(
            "👋 *Expense Bot Ready!*\n\n"
            "Just send me:\n"
            "• A message like `Lunch 45000`\n"
            "• Or a photo of your receipt\n\n"
            "I'll categorize and save it to your Google Sheet automatically! 📊",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("⏳ Processing...")

    try:
        parsed = parse_text_expense(text)
        if parsed.get("not_expense"):
            await update.message.reply_text("🤔 That doesn't look like an expense. Try: `Coffee 25000`", parse_mode="Markdown")
            return
        save_to_sheet(parsed, source="text")
        await update.message.reply_text(format_reply(parsed), parse_mode="Markdown")
    except Exception as e:
        import traceback
        logger.error(f"Text parse error: {e}\nFull traceback:\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await update.message.reply_text("📸 Reading your receipt...")

    try:
        photo = update.message.photo[-1]  # highest resolution
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        parsed = parse_receipt_image(bytes(image_bytes))
        if parsed.get("not_expense"):
            await update.message.reply_text("🤔 I couldn't find expense info in this image.")
            return
        save_to_sheet(parsed, source="receipt")
        await update.message.reply_text(format_reply(parsed), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Photo parse error: {e}")
        await update.message.reply_text("❌ Couldn't read the receipt. Try a clearer photo.")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot is running...")
    app.run_polling()
