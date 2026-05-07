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
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
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

# Conversation state for /setbudget
SETBUDGET_STATE = 1

# ── Categories ────────────────────────────────────────────────────────────────
CATEGORIES = [
    "Coffee",
    "Food & Drinks",
    "Transport",
    "Shopping",
    "Health & Beauty",
    "Entertainment",
    "Utilities & Bills",
    "Travel",
    "Education",
    "Business",
    "Other",
]

EMOJI_MAP = {
    "Coffee":           "☕",
    "Food & Drinks":    "🍜",
    "Transport":        "🚗",
    "Shopping":         "🛍️",
    "Health & Beauty":  "💊",
    "Entertainment":    "🎬",
    "Utilities & Bills":"💡",
    "Travel":           "✈️",
    "Education":        "📚",
    "Business":         "💼",
    "Other":            "📌",
}

# ── Google Sheets ─────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_workbook():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def get_sheet():
    sh = get_workbook()
    try:
        ws = sh.worksheet("Expenses")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Expenses", rows=2000, cols=12)
        ws.append_row(["Date", "Time", "Amount", "Currency", "Category",
                        "Description", "Location", "Notes", "Source"])
        ws.format("A1:I1", {
            "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.4},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
    return ws

def get_budget_sheet():
    sh = get_workbook()
    try:
        ws = sh.worksheet("Budgets")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Budgets", rows=50, cols=4)
        ws.append_row(["Month", "Category", "Budget"])
        ws.format("A1:C1", {
            "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.8},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
    return ws

def get_all_expenses():
    return get_sheet().get_all_records()

def save_to_sheet(parsed: dict, source: str = "text"):
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

def delete_last_row():
    ws = get_sheet()
    all_rows = ws.get_all_values()
    if len(all_rows) <= 1:
        return None
    last = all_rows[-1]
    ws.delete_rows(len(all_rows))
    return last

def save_budgets(month: str, budgets: dict):
    ws = get_budget_sheet()
    all_rows = ws.get_all_records()
    # Remove existing entries for this month
    rows_to_keep = [r for r in all_rows if r.get("Month") != month]
    ws.clear()
    ws.append_row(["Month", "Category", "Budget"])
    for r in rows_to_keep:
        ws.append_row([r["Month"], r["Category"], r["Budget"]])
    for cat, amt in budgets.items():
        if amt and amt > 0:
            ws.append_row([month, cat, amt])

def load_budgets(month: str) -> dict:
    try:
        ws = get_budget_sheet()
        rows = ws.get_all_records()
        return {
            r["Category"]: float(r["Budget"])
            for r in rows
            if r.get("Month") == month and r.get("Budget")
        }
    except:
        return {}

# ── Claude AI ─────────────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

PARSE_PROMPT = f"""You are an expense parsing assistant. Return ONLY valid JSON, no markdown, no explanation.

IMPORTANT: "Coffee" is its own category — use it for any coffee drink purchase.

Categories: {", ".join(CATEGORIES)}

Return this exact JSON:
{{
  "amount": <number or null>,
  "currency": "{CURRENCY}",
  "category": "<one of the categories above>",
  "description": "<what was bought, max 40 chars>",
  "location": "<place or merchant name, empty string if not mentioned>",
  "notes": "<extra context or empty string>",
  "confidence": "high|medium|low"
}}

If NOT an expense, return: {{"not_expense": true}}
"""

ROAST_PROMPT = """You are a funny, savage-but-loveable financial roast bot.
Given a spending, write ONE short roast (max 15 words). Be witty like a best friend teasing them.
Mix English and Indonesian naturally (e.g. 'bro', 'bestie', 'gila', 'anjir').
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
    logger.info(f"Parse: {raw}")
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
    logger.info(f"Receipt: {raw}")
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
def check_budget(category: str, new_amount: float) -> str:
    month = datetime.now().strftime("%Y-%m")
    budgets = load_budgets(month)
    limit = budgets.get(category, 0)
    if not limit:
        return ""
    try:
        rows = get_all_expenses()
        month_total = sum(
            float(r.get("Amount", 0) or 0) for r in rows
            if r.get("Category") == category
            and str(r.get("Date", "")).startswith(month)
        ) + new_amount
        pct = (month_total / limit) * 100
        if pct >= 100:
            return f"🚨 *Budget blown!* {CURRENCY} {int(month_total):,} / {int(limit):,} on {category} ({int(pct)}%)"
        elif pct >= 80:
            return f"⚠️ *Budget warning!* {int(pct)}% of {category} budget used this month"
    except Exception as e:
        logger.error(f"Budget check error: {e}")
    return ""

# ── Summary builders ──────────────────────────────────────────────────────────
def build_summary(rows, title: str, budgets: dict = None) -> str:
    if not rows:
        return f"📭 No expenses for *{title}*"
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
        line = f"{e} *{cat}*\n`{bar}` {CURRENCY} {int(total):,}"
        if budgets and cat in budgets:
            budget = budgets[cat]
            pct = int((total / budget) * 100)
            line += f" / {CURRENCY} {int(budget):,} _{pct}%_"
        lines.append(line)
    lines.append(f"\n💸 *Total: {CURRENCY} {int(grand):,}*")
    return "\n".join(lines)

def get_daily_summary() -> str:
    try:
        rows = get_all_expenses()
        today = datetime.now().strftime("%Y-%m-%d")
        day_rows = [r for r in rows if str(r.get("Date", "")) == today]
        title = f"Daily Recap — {datetime.now().strftime('%d %b %Y')}"
        return build_summary(day_rows, title)
    except Exception as e:
        logger.error(f"Daily summary error: {e}\n{traceback.format_exc()}")
        return "❌ Couldn't load daily recap."

def get_weekly_summary() -> str:
    try:
        rows = get_all_expenses()
        today = datetime.now().date()
        # Start from Monday of current week
        monday = today - timedelta(days=today.weekday())
        week_rows = [r for r in rows if str(r.get("Date", "")) >= str(monday)]
        title = f"Weekly Recap — {monday.strftime('%d %b')} to {today.strftime('%d %b %Y')}"
        return build_summary(week_rows, title)
    except Exception as e:
        logger.error(f"Weekly summary error: {e}\n{traceback.format_exc()}")
        return "❌ Couldn't load weekly recap."

def get_monthly_summary() -> str:
    try:
        rows = get_all_expenses()
        now = datetime.now()
        month = now.strftime("%Y-%m")
        month_rows = [r for r in rows if str(r.get("Date", "")).startswith(month)]
        budgets = load_budgets(month)
        title = f"Monthly Summary — {now.strftime('%B %Y')}"
        return build_summary(month_rows, title, budgets=budgets)
    except Exception as e:
        logger.error(f"Monthly summary error: {e}\n{traceback.format_exc()}")
        return "❌ Couldn't load monthly summary."

# ── Format expense reply ──────────────────────────────────────────────────────
def format_reply(parsed: dict, roast: str = "", budget_warn: str = "") -> str:
    amount    = parsed.get("amount")
    cat       = parsed.get("category", "Other")
    desc      = parsed.get("description", "")
    location  = parsed.get("location", "")
    notes     = parsed.get("notes", "")
    conf      = parsed.get("confidence", "high")
    amount_str = f"{CURRENCY} {int(amount):,}" if amount else "Amount unclear"
    cat_emoji  = EMOJI_MAP.get(cat, "📌")

    msg = "✅ *Saved!*\n\n"
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

# ── Auth ──────────────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return not ALLOWED_USER_ID or user_id == ALLOWED_USER_ID

# ── /setbudget conversation ───────────────────────────────────────────────────
# We store budgets being built in context.user_data
async def cmd_setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    context.user_data["budgets_input"] = {}
    context.user_data["budget_cats"] = list(CATEGORIES)
    context.user_data["budget_month"] = datetime.now().strftime("%Y-%m")
    month_label = datetime.now().strftime("%B %Y")

    existing = load_budgets(context.user_data["budget_month"])
    context.user_data["budgets_input"] = dict(existing)

    await update.message.reply_text(
        f"💰 *Setting budgets for {month_label}*\n\n"
        "I'll ask you one by one. Type the amount in IDR, or send `0` to skip a category.\n\n"
        "Let's start! ☕ What's your *Coffee* budget this month?",
        parse_mode="Markdown"
    )
    context.user_data["budget_idx"] = 0
    return SETBUDGET_STATE

async def budget_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(".", "")
    cats = context.user_data["budget_cats"]
    idx  = context.user_data["budget_idx"]

    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text("Please send a number (e.g. `500000`) or `0` to skip.")
        return SETBUDGET_STATE

    current_cat = cats[idx]
    if amount > 0:
        context.user_data["budgets_input"][current_cat] = amount

    idx += 1
    context.user_data["budget_idx"] = idx

    if idx < len(cats):
        next_cat = cats[idx]
        emoji = EMOJI_MAP.get(next_cat, "📌")
        existing = context.user_data["budgets_input"].get(next_cat, 0)
        hint = f" _(currently {CURRENCY} {int(existing):,})_" if existing else ""
        await update.message.reply_text(
            f"{emoji} What's your *{next_cat}* budget this month?{hint}\n_(Send `0` to skip)_",
            parse_mode="Markdown"
        )
        return SETBUDGET_STATE
    else:
        # Done — save all
        month = context.user_data["budget_month"]
        budgets = context.user_data["budgets_input"]
        save_budgets(month, budgets)

        lines = ["✅ *Budgets saved!*\n"]
        for cat in cats:
            if cat in budgets:
                e = EMOJI_MAP.get(cat, "📌")
                lines.append(f"{e} {cat}: {CURRENCY} {int(budgets[cat]):,}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return ConversationHandler.END

async def cancel_setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Budget setup cancelled.")
    return ConversationHandler.END

# ── Monthly budget prompt (auto on 1st) ───────────────────────────────────────
async def prompt_monthly_budget(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_USER_ID:
        return
    month_label = datetime.now().strftime("%B %Y")
    await context.bot.send_message(
        chat_id=ALLOWED_USER_ID,
        text=f"📅 It's a new month! Want to set your budgets for *{month_label}*?\n\nType /setbudget to get started!",
        parse_mode="Markdown"
    )

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *Expense Bot*\n\n"
        "Send any expense naturally:\n"
        "`Ngopi di Stuja 35000`\n"
        "`Grab ke kantor 45000`\n"
        "Or send a 📸 receipt photo\n\n"
        "*Commands:*\n"
        "/daily — today's spending\n"
        "/weekly — this week (Mon–today)\n"
        "/summary — this month\n"
        "/setbudget — set monthly budgets\n"
        "/undo — delete last entry\n",
        parse_mode="Markdown"
    )

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text("⏳ Loading...")
    await update.message.reply_text(get_daily_summary(), parse_mode="Markdown")

async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text("⏳ Loading...")
    await update.message.reply_text(get_weekly_summary(), parse_mode="Markdown")

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text("⏳ Loading...")
    await update.message.reply_text(get_monthly_summary(), parse_mode="Markdown")

async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
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

# ── Message handlers ──────────────────────────────────────────────────────────
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
                "🤔 Doesn't look like an expense.\nTry: `Kopi 25000` or `Grab 45000`",
                parse_mode="Markdown"
            )
            return
        save_to_sheet(parsed, source="text")
        roast      = get_roast(parsed)
        budget_warn = check_budget(parsed.get("category", ""), float(parsed.get("amount") or 0))
        await update.message.reply_text(
            format_reply(parsed, roast=roast, budget_warn=budget_warn),
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
        roast       = get_roast(parsed)
        budget_warn = check_budget(parsed.get("category", ""), float(parsed.get("amount") or 0))
        await update.message.reply_text(
            format_reply(parsed, roast=roast, budget_warn=budget_warn),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Photo error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Couldn't read receipt. Try a clearer photo.")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # /setbudget conversation
    budget_conv = ConversationHandler(
        entry_points=[CommandHandler("setbudget", cmd_setbudget)],
        states={SETBUDGET_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, budget_input_handler)]},
        fallbacks=[CommandHandler("cancel", cancel_setbudget)],
    )

    app.add_handler(budget_conv)
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("undo",    cmd_undo))
    app.add_handler(CallbackQueryHandler(handle_undo_callback, pattern="^undo_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Auto-prompt budget setup on 1st of every month at 8AM WIB (01:00 UTC)
    if ALLOWED_USER_ID:
        app.job_queue.run_monthly(
            prompt_monthly_budget,
            when=datetime.strptime("01:00", "%H:%M").time(),
            day=1,
        )
        logger.info("Monthly budget prompt scheduled for 1st of month 8AM WIB")

    logger.info("Bot is running...")
    app.run_polling()
