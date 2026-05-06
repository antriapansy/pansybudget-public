import os
import json
import base64
import logging
import traceback
import anthropic
import gspread
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes, JobQueue
)
from google.oauth2.service_account import Credentials

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID  = os.environ["GOOGLE_SPREADSHEET_ID"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", "0"))
CURRENCY        = os.environ.get("CURRENCY", "IDR")

BUDGETS = {
    "Food & Drinks":  int(os.environ.get("BUDGET_FOOD", "0")),
    "Transport":      int(os.environ.get("BUDGET_TRANSPORT", "0")),
    "Shopping":       int(os.environ.get("BUDGET_SHOPPING", "0")),
    "Entertainment":  int(os.environ.get("BUDGET_ENTERTAINMENT", "0")),
}

# ── Google Sheets ─────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet("Expenses")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Expenses", rows=2000, cols=12)
        ws.append_row(["Date", "Time", "Amount", "Currency", "Category", "Description", "Location", "Notes", "Source"])
        ws.format("A1:I1", {
            "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.4},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
    return ws

def get_all_expenses():
    return get_sheet().get_all_records()

def save_to_sheet(parsed: dict, source: str = "text") -> int:
    ws = get_sheet()
    now = datetime.now()
    ws.append_row([
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        parsed.get("amount", ""),
        parsed.get("currency", CURRENCY),
        parsed.get("category", "Other"),
        parsed.get("description", ""),
        parsed.get("location", ""),
        parsed.get("notes", ""),
        source,
    ])
    return len(ws.get_all_values())

def delete_last_row():
    ws = get_sheet()
    all_rows = ws.get_all_values()
    if len(all_rows) <= 1:
        return None
    last = all_rows[-1]
    ws.delete_rows(len(all_rows))
    return last

# ── Claude AI ─────────────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

PARSE_PROMPT = f"""You are an expense parsing assistant. Return ONLY valid JSON, no markdown, no explanation.

Categories:
- Food & Drinks, Transport, Shopping, Health & Beauty, Entertainment,
  Utilities & Bills, Travel, Education, Business, Other

Return this JSON:
{{
  "amount": <number or null>,
  "currency": "{CURRENCY}",
  "category": "<category>",
  "description": "<what was bought, max 40 chars>",
  "location": "<place or merchant name, empty string if not mentioned>",
  "notes": "<extra context or empty string>",
  "confidence": "high|medium|low"
}}

If NOT an expense, return: {{"not_expense": true}}
"""

ROAST_PROMPT = """You are a funny, savage-but-loveable financial roast bot. 
Given a spending, write ONE short roast (max 15 words). Be witty like a best friend.
Mix English and Indonesian naturally if it fits (e.g. 'bro', 'bestie', 'anjir', 'gila').
Return only the roast text, nothing else."""

def clean_json(raw: str) -> str:
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()

def parse_expense(text: str) -> dict:
    r = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=500, system=PARSE_PROMPT,
        messages=[{"role": "user", "content": f"Parse: {text}"}],
    )
    raw = clean_json(r.content[0].text)
    logger.info(f"Parse response: {raw}")
    return json.loads(raw)

def parse_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    r = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=500, system=PARSE_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": "This is a receipt. Extract the expense."},
        ]}],
    )
    raw = clean_json(r.content[0].text)
    logger.info(f"Receipt response: {raw}")
    return json.loads(raw)

def get_roast(parsed: dict) -> str:
    try:
        desc = parsed.get("description", "")
        cat  = parsed.get("category", "")
        amt  = parsed.get("amount", 0)
        loc  = parsed.get("location", "")
        ctx  = f"{desc} at {loc}, {CURRENCY} {int(amt):,}, {cat}" if loc else f"{desc}, {CURRENCY} {int(amt):,}, {cat}"
        r = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=60, system=ROAST_PROMPT,
            messages=[{"role": "user", "content": ctx}],
        )
        return r.content[0].text.strip()
    except:
        return ""

# ── Budget check ──────────────────────────────────────────────────────────────
def check_budget(category: str, new_amount: float) -> str | None:
    limit = BUDGETS.get(category, 0)
    if not limit:
        return None
    try:
        rows = get_all_expenses()
        now = datetime.now()
        month_total = sum(
            float(r.get("Amount", 0) or 0) for r in rows
            if r.get("Category") == category
            and str(r.get("Date", "")).startswith(now.strftime("%Y-%m"))
        ) + new_amount
        pct = (month_total / limit) * 100
        if pct >= 100:
            return f"🚨 *Budget blown!* {CURRENCY} {int(month_total):,} / {int(limit):,} on {category} ({int(pct)}%)"
        elif pct >= 80:
            return f"⚠️ *Budget warning!* {int(pct)}% of {category} budget used this month"
    except Exception as e:
        logger.error(f"Budget check error: {e}")
    return None

# ── Summaries ─────────────────────────────────────────────────────────────────
EMOJI_MAP = {
    "Food & Drinks": "🍜", "Transport": "🚗", "Shopping": "🛍️",
    "Health & Beauty": "💊", "Entertainment": "🎬", "Utilities & Bills": "💡",
    "Travel": "✈️", "Education": "📚", "Business": "💼", "Other": "📌",
}

def build_summary(rows, title: str) -> str:
    if not rows:
        return f"📭 No expenses for {title}!"
    totals = defaultdict(float)
    grand = 0.0
    for r in rows:
        amt = float(r.get("Amount", 0) or 0)
        totals[r.get("Category", "Other")] += amt
        grand += amt

    lines = [f"📊 *{title}*\n"]
    for cat, total in sorted(totals.items(), key=lambda x: -x[1]):
        e = EMOJI_MAP.get(cat, "📌")
        bar_fill = int((total / grand) * 10)
        bar = "█" * bar_fill + "░" * (10 - bar_fill)
        lines.append(f"{e} {cat}\n`{bar}` {CURRENCY} {int(total):,}")
    lines.append(f"\n💸 *Total: {CURRENCY} {int(grand):,}*")
    return "\n".join(lines)

def get_monthly_summary() -> str:
    try:
        rows = get_all_expenses()
        now = datetime.now()
        month_rows = [r for r in rows if str(r.get("Date", "")).startswith(now.strftime("%Y-%m"))]
        return build_summary(month_rows, f"Monthly Summary — {now.strftime('%B %Y')}")
    except Exception as e:
        logger.error(f"Monthly summary error: {e}\n{traceback.format_exc()}")
        return "❌ Couldn't load monthly summary."

def get_weekly_summary() -> str:
    try:
        rows = get_all_expenses()
        today = datetime.now().date()
        week_ago = today - timedelta(days=7)
        week_rows = [
            r for r in rows
            if str(r.get("Date", "")) >= str(week_ago)
        ]
        start_str = week_ago.strftime("%d %b")
        end_str   = today.strftime("%d %b %Y")
        return build_summary(week_rows, f"Weekly Recap — {start_str} to {end_str}")
    except Exception as e:
        logger.error(f"Weekly summary error: {e}\n{traceback.format_exc()}")
        return "❌ Couldn't load weekly summary."

# ── Format expense reply ──────────────────────────────────────────────────────
def format_reply(parsed: dict, roast: str = "", budget_warn: str = "") -> str:
    amount   = parsed.get("amount")
    cat      = parsed.get("category", "Other")
    desc     = parsed.get("description", "")
    location = parsed.get("location", "")
    notes    = parsed.get("notes", "")
    conf     = parsed.get("confidence", "high")
    amount_str = f"{CURRENCY} {int(amount):,}" if amount else "Amount unclear"
    cat_emoji  = EMOJI_MAP.get(cat, "📌")

    msg = f"✅ *Saved!*\n\n"
    msg += f"💰 *{amount_str}*\n"
    msg += f"{cat_emoji} {cat}\n"
    msg += f"📝 {desc}"
    if location:
        msg += f"\n📍 {location}"
    if notes:
        msg += f"\n💬 _{notes}_"
    if conf == "low":
        msg += "\n\n⚠️ _Low confidence — double-check?_"
    if roast:
        msg += f"\n\n_{roast}_"
    if budget_warn:
        msg += f"\n\n{budget_warn}"
    return msg

# ── Auth helper ───────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return not ALLOWED_USER_ID or user_id == ALLOWED_USER_ID

# ── Scheduled weekly recap ────────────────────────────────────────────────────
async def send_weekly_recap(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_USER_ID:
        return
    summary = get_weekly_summary()
    summary += "\n\n_Have a great week ahead! Try not to break the budget 😅_"
    await context.bot.send_message(
        chat_id=ALLOWED_USER_ID,
        text=summary,
        parse_mode="Markdown"
    )

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *Expense Bot — upgraded!*\n\n"
        "Just send me your expense naturally:\n"
        "`Ngopi di Stuja 35000`\n"
        "`Grab ke kantor 45000`\n"
        "Or send a 📸 *photo of your receipt*\n\n"
        "*Commands:*\n"
        "/summary — monthly breakdown\n"
        "/weekly — this week's recap\n"
        "/undo — delete last entry\n"
        "/help — show this message",
        parse_mode="Markdown"
    )

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Loading...")
    await update.message.reply_text(get_monthly_summary(), parse_mode="Markdown")

async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Loading...")
    await update.message.reply_text(get_weekly_summary(), parse_mode="Markdown")

async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    keyboard = [[
        InlineKeyboardButton("✅ Yes, delete it", callback_data="undo_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="undo_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ Delete your last expense entry?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "undo_confirm":
        deleted = delete_last_row()
        if deleted:
            await query.edit_message_text(f"🗑️ Deleted: {' | '.join(str(x) for x in deleted[:6])}")
        else:
            await query.edit_message_text("Nothing to delete!")
    else:
        await query.edit_message_text("Cancelled.")

# ── Message handler ───────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    text = update.message.text.strip()
    await update.message.reply_text("⏳ On it...")

    try:
        parsed = parse_expense(text)
        if parsed.get("not_expense"):
            await update.message.reply_text(
                "🤔 That doesn't look like an expense.\nTry: `Kopi 25000` or `Grab 45000`",
                parse_mode="Markdown"
            )
            return

        save_to_sheet(parsed, source="text")
        roast = get_roast(parsed)
        budget_warn = check_budget(parsed.get("category", ""), float(parsed.get("amount") or 0))
        await update.message.reply_text(
            format_reply(parsed, roast=roast, budget_warn=budget_warn or ""),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Text error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await update.message.reply_text("📸 Reading receipt...")

    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        parsed = parse_receipt(bytes(image_bytes))
        if parsed.get("not_expense"):
            await update.message.reply_text("🤔 Couldn't find expense info in this image.")
            return

        save_to_sheet(parsed, source="receipt")
        roast = get_roast(parsed)
        budget_warn = check_budget(parsed.get("category", ""), float(parsed.get("amount") or 0))
        await update.message.reply_text(
            format_reply(parsed, roast=roast, budget_warn=budget_warn or ""),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Photo error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Couldn't read the receipt. Try a clearer photo.")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("undo",    cmd_undo))
    app.add_handler(CallbackQueryHandler(handle_undo_callback, pattern="^undo_"))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Weekly recap every Sunday at 8PM (UTC+7 = 13:00 UTC)
    if ALLOWED_USER_ID:
        app.job_queue.run_daily(
            send_weekly_recap,
            time=datetime.strptime("13:00", "%H:%M").time(),
            days=(6,),  # 6 = Sunday
        )
        logger.info("Weekly recap scheduled for Sundays 8PM WIB")

    logger.info("Bot is running...")
    app.run_polling()
