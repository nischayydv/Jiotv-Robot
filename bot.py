import os
import re
import json
import logging
import asyncio
from urllib.parse import quote
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from threading import Thread
import aiohttp

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
PORT = int(os.environ.get('PORT', 5000))
WEBAPP_URL = os.environ.get('WEBAPP_URL', f'https://your-app.herokuapp.com')

# Gemini AI Setup
gemini_model = None
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("✅ Gemini AI enabled")
    except:
        logger.warning("⚠️ Gemini not available, using basic categorization")

# Data storage
channels_data = {}
categories = {}
bot_stats = {
    'total_users': set(),
    'total_plays': 0,
    'last_updated': None
}
bot_settings = {
    'bot_name': 'Live TV Bot',
    'welcome_message': '🎬 Welcome! Watch live TV channels.',
    'maintenance_mode': False
}

# Flask App
app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)

@app.route('/')
def index():
    return render_template('index.html', channels=list(channels_data.values()))

@app.route('/player')
def player():
    channel_id = request.args.get('id', '')
    return render_template('player.html', channel_id=channel_id)

@app.route('/api/channels')
def api_channels():
    """API endpoint for channels in correct format"""
    formatted = []
    for cid, ch in channels_data.items():
        formatted.append({
            'id': cid,
            'name': ch['name'],
            'logo': ch.get('logo', ''),
            'link': ch.get('link', ch.get('url', '')),
            'drmScheme': ch.get('drmScheme', ''),
            'drmLicense': ch.get('drmLicense', ''),
            'cookie': ch.get('cookie', ''),
            'category': ch.get('category', 'Other'),
            'updated_at': ch.get('updated_at', datetime.now().isoformat())
        })
    return jsonify(formatted)

@app.route('/api/channel/<channel_id>')
def api_channel(channel_id):
    """Get single channel data"""
    ch = channels_data.get(channel_id, {})
    if not ch:
        return jsonify({'error': 'Channel not found'}), 404
    
    return jsonify({
        'id': channel_id,
        'name': ch['name'],
        'logo': ch.get('logo', ''),
        'link': ch.get('link', ch.get('url', '')),
        'drmScheme': ch.get('drmScheme', ''),
        'drmLicense': ch.get('drmLicense', ''),
        'cookie': ch.get('cookie', ''),
        'category': ch.get('category', 'Other')
    })

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'channels': len(channels_data),
        'categories': len(categories),
        'gemini': gemini_model is not None
    })

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def categorize_with_ai(channel_name):
    """Smart categorization with Gemini or fallback"""
    if gemini_model:
        try:
            prompt = f"""Categorize this TV channel into EXACTLY ONE category from this list:
Sports, News, Entertainment, Movies, Music, Kids, Documentary, Religious, Regional, Other

Channel: {channel_name}

Respond with ONLY the category name."""
            
            response = await asyncio.to_thread(gemini_model.generate_content, prompt)
            category = response.text.strip()
            
            valid = ['Sports', 'News', 'Entertainment', 'Movies', 'Music', 'Kids', 
                    'Documentary', 'Religious', 'Regional', 'Other']
            
            if category in valid:
                return category
        except Exception as e:
            logger.error(f"Gemini error: {e}")
    
    # Fallback to keyword matching
    return categorize_basic(channel_name)

def categorize_basic(name):
    """Basic keyword-based categorization"""
    n = name.lower()
    
    keywords = {
        'Sports': ['sport', 'cricket', 'football', 'fifa', 'hockey', 'espn', 'star sports', 'sony ten'],
        'News': ['news', 'ndtv', 'aaj tak', 'abp', 'zee news', 'india today', 'republic', 'times now'],
        'Movies': ['movie', 'cinema', 'pictures', 'pix', 'flix', 'max', 'hbo'],
        'Music': ['music', 'mtv', '9xm', 'zoom', 'vh1', 'bindass'],
        'Kids': ['kids', 'cartoon', 'nick', 'pogo', 'disney', 'sonic', 'hungama'],
        'Documentary': ['discovery', 'national geo', 'nat geo', 'animal planet', 'history', 'tlc'],
        'Entertainment': ['star', 'sony', 'zee', 'colors', '&tv', 'sab', 'bharat', 'plus'],
        'Religious': ['aastha', 'sanskar', 'god', 'ishwar', 'devotional']
    }
    
    for category, words in keywords.items():
        if any(word in n for word in words):
            return category
    
    return 'Other'

def parse_json_channels(content):
    """Parse JSON format channels"""
    global channels_data, categories
    
    try:
        data = json.loads(content) if isinstance(content, str) else content
        channels_data.clear()
        
        # Handle list format
        if isinstance(data, list):
            for idx, ch in enumerate(data):
                cid = f"ch_{idx}"
                channels_data[cid] = {
                    'name': ch.get('name', 'Unknown'),
                    'link': ch.get('link', ch.get('url', '')),
                    'logo': ch.get('logo', ''),
                    'drmScheme': ch.get('drmScheme', ''),
                    'drmLicense': ch.get('drmLicense', ''),
                    'cookie': ch.get('cookie', ''),
                    'category': ch.get('category'),
                    'updated_at': ch.get('updated_at', datetime.now().isoformat()),
                    'needs_category': not ch.get('category')
                }
        
        # Handle dict with channels key
        elif isinstance(data, dict) and 'channels' in data:
            for idx, ch in enumerate(data['channels']):
                cid = f"ch_{idx}"
                channels_data[cid] = {
                    'name': ch.get('name', 'Unknown'),
                    'link': ch.get('link', ch.get('url', '')),
                    'logo': ch.get('logo', ''),
                    'drmScheme': ch.get('drmScheme', ''),
                    'drmLicense': ch.get('drmLicense', ''),
                    'cookie': ch.get('cookie', ''),
                    'category': ch.get('category'),
                    'updated_at': ch.get('updated_at', datetime.now().isoformat()),
                    'needs_category': not ch.get('category')
                }
        
        logger.info(f"✅ Loaded {len(channels_data)} channels")
        return True
    
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return False

async def auto_categorize_all():
    """Auto-categorize channels without category"""
    uncategorized = [(cid, ch) for cid, ch in channels_data.items() 
                     if ch.get('needs_category', False)]
    
    if not uncategorized:
        organize_categories()
        return
    
    logger.info(f"🤖 Categorizing {len(uncategorized)} channels...")
    
    for idx, (cid, ch) in enumerate(uncategorized, 1):
        try:
            category = await categorize_with_ai(ch['name'])
            ch['category'] = category
            ch['needs_category'] = False
            logger.info(f"[{idx}/{len(uncategorized)}] {ch['name']} → {category}")
            
            if gemini_model:
                await asyncio.sleep(1)  # Rate limit for Gemini
        except Exception as e:
            logger.error(f"Categorization error: {e}")
            ch['category'] = 'Other'
    
    organize_categories()
    logger.info("✅ Categorization complete!")

def organize_categories():
    """Group channels by category"""
    global categories
    categories.clear()
    
    for cid, ch in channels_data.items():
        cat = ch.get('category', 'Other')
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(cid)
    
    logger.info(f"📂 Organized into {len(categories)} categories")

async def load_from_url(url):
    """Load playlist from URL"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    return await response.text()
                logger.error(f"Failed to load: HTTP {response.status}")
                return None
    except Exception as e:
        logger.error(f"URL load error: {e}")
        return None

# Telegram Bot Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_stats['total_users'].add(user.id)
    
    if bot_settings['maintenance_mode'] and not is_admin(user.id):
        await update.message.reply_text("🔧 Bot under maintenance!")
        return
    
    keyboard = []
    for cat in sorted(categories.keys()):
        keyboard.append([InlineKeyboardButton(
            f"📺 {cat} ({len(categories[cat])})", 
            callback_data=f"cat_{cat}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔍 Search", switch_inline_query_current_chat="")])
    
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin")])
    
    text = f"""
🎬 <b>{bot_settings['bot_name']}</b>

👋 Hi {user.first_name}!

📺 Channels: {len(channels_data)}
🗂 Categories: {len(categories)}

Select a category or search:
"""
    
    await update.message.reply_text(
        text, 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    cat = query.data.replace('cat_', '')
    channel_ids = categories.get(cat, [])
    
    keyboard = []
    for cid in channel_ids[:50]:
        ch = channels_data[cid]
        keyboard.append([InlineKeyboardButton(
            f"▶️ {ch['name']}", 
            callback_data=f"play_{cid}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start")])
    
    await query.message.edit_text(
        f"📺 <b>{cat}</b>\n\n{len(channel_ids)} channels:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def play_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Opening player...")
    
    cid = query.data.replace('play_', '')
    ch = channels_data.get(cid)
    
    if not ch:
        await query.answer("Channel not found!", show_alert=True)
        return
    
    bot_stats['total_plays'] += 1
    
    player_url = f"{WEBAPP_URL}/player?id={cid}"
    
    keyboard = [
        [InlineKeyboardButton("🎬 Open Player", web_app=WebAppInfo(url=player_url))],
        [InlineKeyboardButton("🔗 Direct Link", url=player_url)],
        [InlineKeyboardButton("🔙 Back", callback_data=f"cat_{ch.get('category', 'Other')}")]
    ]
    
    await query.message.edit_text(
        f"🎬 <b>{ch['name']}</b>\n\n📂 {ch.get('category', 'Other')}\n\nClick to watch:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized!", show_alert=True)
        return
    
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("📤 Upload JSON", callback_data="admin_upload")],
        [InlineKeyboardButton("🔄 Load URL", callback_data="admin_url")],
        [InlineKeyboardButton("🤖 AI Categorize", callback_data="admin_categorize")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="start")]
    ]
    
    text = f"""
⚙️ <b>Admin Panel</b>

📺 Channels: {len(channels_data)}
🗂 Categories: {len(categories)}
👥 Users: {len(bot_stats['total_users'])}
▶️ Plays: {bot_stats['total_plays']}
🤖 Gemini: {'✅' if gemini_model else '❌'}
📅 Updated: {bot_stats['last_updated'].strftime('%Y-%m-%d %H:%M') if bot_stats['last_updated'] else 'Never'}
"""
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only!")
        return
    
    file = update.message.document
    if not file or not file.file_name.endswith('.json'):
        await update.message.reply_text("❌ Send .json file only!")
        return
    
    await update.message.reply_text("⏳ Processing...")
    
    try:
        file_obj = await context.bot.get_file(file.file_id)
        content = await file_obj.download_as_bytearray()
        
        if parse_json_channels(content.decode('utf-8')):
            await auto_categorize_all()
            bot_stats['last_updated'] = datetime.now()
            
            await update.message.reply_text(
                f"✅ Success!\n\n📺 {len(channels_data)} channels\n🗂 {len(categories)} categories"
            )
        else:
            await update.message.reply_text("❌ Invalid JSON!")
    
    except Exception as e:
        logger.error(f"File error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "start":
        await start(update, context)
    elif data.startswith("cat_"):
        await category_handler(update, context)
    elif data.startswith("play_"):
        await play_handler(update, context)
    elif data == "admin":
        await admin_handler(update, context)
    elif data == "admin_categorize":
        await query.answer("Starting categorization...", show_alert=True)
        await auto_categorize_all()
        await admin_handler(update, context)
    elif data == "admin_upload":
        await query.answer()
        await query.message.edit_text(
            "📤 Send me a .json file\n\nFormat:\n<code>[{\"name\":\"Channel\",\"link\":\"url\",\"logo\":\"url\",\"drmScheme\":\"clearkey\",\"drmLicense\":\"key:id\",\"cookie\":\"cookie\"}]</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_url":
        context.user_data['awaiting_url'] = True
        await query.message.edit_text(
            "🔄 Send JSON URL:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_stats":
        cat_list = "\n".join([f"• {c}: {len(ch)}" for c, ch in sorted(categories.items())[:10]])
        await query.message.edit_text(
            f"📊 <b>Statistics</b>\n\n{cat_list}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_url') and is_admin(update.effective_user.id):
        context.user_data['awaiting_url'] = False
        await update.message.reply_text("⏳ Loading...")
        
        content = await load_from_url(update.message.text)
        if content and parse_json_channels(content):
            await auto_categorize_all()
            bot_stats['last_updated'] = datetime.now()
            await update.message.reply_text(
                f"✅ Loaded!\n\n📺 {len(channels_data)} channels\n🗂 {len(categories)} categories"
            )
        else:
            await update.message.reply_text("❌ Failed to load!")

def main():
    # Start Flask
    Thread(target=run_flask, daemon=True).start()
    
    # Start Bot
    app_bot = Application.builder().token(BOT_TOKEN).build()
    
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(callback_router))
    app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    logger.info("🚀 Bot started!")
    logger.info(f"📡 Server: {WEBAPP_URL}")
    logger.info(f"🤖 Gemini: {'ON' if gemini_model else 'OFF'}")
    
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
