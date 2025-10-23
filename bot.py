import os
import re
import json
import logging
import asyncio
from urllib.parse import quote
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from flask import Flask, render_template, send_from_directory, jsonify
from threading import Thread
import aiohttp
import google.generativeai as genai

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'YOUR_GEMINI_API_KEY')
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
PORT = int(os.environ.get('PORT', 5000))
WEBAPP_URL = os.environ.get('WEBAPP_URL', f'http://localhost:{PORT}')

# Configure Gemini
if GEMINI_API_KEY and GEMINI_API_KEY != 'YOUR_GEMINI_API_KEY':
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-pro')
else:
    gemini_model = None
    logger.warning("Gemini API key not configured. AI categorization disabled.")

# Data storage
channels_data = {}
categories = {}
bot_stats = {
    'total_users': set(),
    'total_plays': 0,
    'last_updated': None
}
bot_settings = {
    'bot_name': 'Jio TV Bot',
    'welcome_message': '🎬 Welcome to Jio TV Bot! Watch unlimited live TV channels.',
    'maintenance_mode': False
}

# Flask App
app = Flask(__name__, template_folder='templates', static_folder='static')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/player')
def player():
    return render_template('player.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'channels': len(channels_data)}), 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def categorize_with_gemini(channel_name):
    """Use Gemini AI to categorize channels intelligently"""
    if not gemini_model:
        return "Other"
    
    try:
        prompt = f"""Categorize this TV channel into ONE of these categories ONLY:
Categories: Sports, News, Entertainment, Movies, Music, Kids, Documentary, Religious, Regional, Other

Channel Name: {channel_name}

Reply with ONLY the category name, nothing else."""
        
        response = gemini_model.generate_content(prompt)
        category = response.text.strip()
        
        # Validate category
        valid_categories = ['Sports', 'News', 'Entertainment', 'Movies', 'Music', 'Kids', 
                          'Documentary', 'Religious', 'Regional', 'Other']
        if category in valid_categories:
            return category
        return "Other"
    except Exception as e:
        logger.error(f"Gemini categorization error: {e}")
        return "Other"

def parse_m3u_playlist(m3u_content):
    """Parse M3U playlist"""
    global channels_data, categories
    channels_data = {}
    categories = {}
    
    lines = m3u_content.strip().split('\n')
    current_channel = {}
    
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            group_match = re.search(r'group-title="([^"]+)"', line)
            category = group_match.group(1) if group_match else None
            
            name_parts = line.split(',', 1)
            channel_name = name_parts[1].strip() if len(name_parts) > 1 else "Unknown"
            
            logo_match = re.search(r'tvg-logo="([^"]+)"', line)
            logo = logo_match.group(1) if logo_match else ""
            
            current_channel = {
                'name': channel_name,
                'category': category,
                'logo': logo,
                'needs_categorization': category is None
            }
        elif line and not line.startswith('#') and current_channel:
            current_channel['url'] = line
            channel_id = f"ch_{len(channels_data)}"
            channels_data[channel_id] = current_channel
            current_channel = {}

async def auto_categorize_channels():
    """Auto-categorize channels without category using Gemini"""
    if not gemini_model:
        logger.info("Gemini not configured, using basic categorization")
        for channel_id, channel in channels_data.items():
            if channel.get('needs_categorization'):
                channel['category'] = "Other"
        organize_categories()
        return
    
    logger.info("Starting AI categorization with Gemini...")
    uncategorized = [ch for ch in channels_data.values() if ch.get('needs_categorization')]
    
    for i, channel in enumerate(uncategorized, 1):
        try:
            category = await categorize_with_gemini(channel['name'])
            channel['category'] = category
            channel['needs_categorization'] = False
            logger.info(f"[{i}/{len(uncategorized)}] {channel['name']} → {category}")
            await asyncio.sleep(0.5)  # Rate limiting
        except Exception as e:
            logger.error(f"Error categorizing {channel['name']}: {e}")
            channel['category'] = "Other"
    
    organize_categories()
    logger.info("AI categorization completed!")

def organize_categories():
    """Organize channels into categories"""
    global categories
    categories = {}
    for channel_id, channel in channels_data.items():
        cat = channel.get('category', 'Other')
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(channel_id)

def parse_json_playlist(json_content):
    """Parse JSON playlist format"""
    global channels_data, categories
    
    try:
        data = json.loads(json_content)
        channels_data = {}
        categories = {}
        
        if isinstance(data, dict) and 'channels' in data:
            for idx, channel in enumerate(data['channels']):
                channel_id = f"ch_{idx}"
                channels_data[channel_id] = {
                    'name': channel.get('name', 'Unknown'),
                    'url': channel.get('url', ''),
                    'logo': channel.get('logo', ''),
                    'category': channel.get('category', 'Other')
                }
        elif isinstance(data, list):
            for idx, channel in enumerate(data):
                channel_id = f"ch_{idx}"
                channels_data[channel_id] = {
                    'name': channel.get('name', 'Unknown'),
                    'url': channel.get('url', ''),
                    'logo': channel.get('logo', ''),
                    'category': channel.get('category', 'Other')
                }
        
        organize_categories()
        return True
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return False

async def load_playlist_from_url(url):
    """Load playlist from URL"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    content = await response.text()
                    return content
                else:
                    logger.error(f"Failed to load playlist: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"Error loading playlist: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    bot_stats['total_users'].add(user.id)
    
    if bot_settings['maintenance_mode'] and not is_admin(user.id):
        await update.message.reply_text(
            "🔧 <b>Maintenance Mode</b>\n\nBot is under maintenance. Please try again later.",
            parse_mode='HTML'
        )
        return
    
    keyboard = []
    sorted_categories = sorted(categories.keys())
    
    for i in range(0, len(sorted_categories), 2):
        row = []
        row.append(InlineKeyboardButton(
            f"📺 {sorted_categories[i]}", 
            callback_data=f"cat_{sorted_categories[i]}"
        ))
        if i + 1 < len(sorted_categories):
            row.append(InlineKeyboardButton(
                f"📺 {sorted_categories[i+1]}", 
                callback_data=f"cat_{sorted_categories[i+1]}"
            ))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔍 Search", switch_inline_query_current_chat="")])
    
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = f"""
{bot_settings['welcome_message']}

👋 Hi <b>{user.first_name}</b>!

📺 Total Channels: {len(channels_data)}
🗂 Categories: {len(categories)}
🔍 Search for channels

Credits: @NY_BOTS
"""
    
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await update.callback_query.message.edit_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel"""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized!", show_alert=True)
        return
    
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("📤 Upload M3U File", callback_data="admin_upload_m3u")],
        [InlineKeyboardButton("📋 Upload JSON File", callback_data="admin_upload_json")],
        [InlineKeyboardButton("🔄 Reload from URL", callback_data="admin_reload_url")],
        [InlineKeyboardButton("🤖 AI Categorize", callback_data="admin_ai_categorize")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔧 Maintenance", callback_data="admin_maintenance")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    admin_text = f"""
⚙️ <b>Admin Panel</b>

📺 Channels: {len(channels_data)}
🗂 Categories: {len(categories)}
👥 Users: {len(bot_stats['total_users'])}
▶️ Plays: {bot_stats['total_plays']}
📅 Updated: {bot_stats['last_updated'].strftime('%Y-%m-%d %H:%M') if bot_stats['last_updated'] else 'Never'}

🤖 Gemini AI: {'✅ Active' if gemini_model else '❌ Not configured'}
🔧 Maintenance: {'✅ ON' if bot_settings['maintenance_mode'] else '❌ OFF'}
"""
    
    await query.message.edit_text(admin_text, reply_markup=reply_markup, parse_mode='HTML')

async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection"""
    query = update.callback_query
    await query.answer()
    
    category = query.data.replace('cat_', '')
    channel_ids = categories.get(category, [])
    
    keyboard = []
    for channel_id in channel_ids[:50]:  # Limit to 50 channels per page
        channel = channels_data[channel_id]
        keyboard.append([InlineKeyboardButton(
            f"▶️ {channel['name']}", 
            callback_data=f"play_{channel_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(
        f"📺 <b>{category}</b>\n\n{len(channel_ids)} channels available:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def play_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel play"""
    query = update.callback_query
    await query.answer("Opening player...")
    
    channel_id = query.data.replace('play_', '')
    channel = channels_data.get(channel_id)
    
    if not channel:
        await query.answer("Channel not found!", show_alert=True)
        return
    
    bot_stats['total_plays'] += 1
    
    player_data = {
        'url': channel['url'],
        'title': channel['name'],
        'logo': channel['logo'],
        'channel_id': channel_id
    }
    
    encoded_data = quote(json.dumps(player_data))
    webapp_url = f"{WEBAPP_URL}/player?data={encoded_data}"
    
    keyboard = [
        [InlineKeyboardButton("🎬 Open Player", web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton("🔙 Back", callback_data=f"cat_{channel['category']}")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    info_text = f"""
🎬 <b>{channel['name']}</b>

📂 Category: {channel['category']}
📺 Click below to watch

<i>🔒 Secure player - URLs hidden</i>
"""
    
    await query.message.edit_text(info_text, reply_markup=reply_markup, parse_mode='HTML')

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle M3U/JSON file uploads"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Only admins can upload files!")
        return
    
    file = update.message.document
    if not file:
        return
    
    file_name = file.file_name.lower()
    
    if not (file_name.endswith('.m3u') or file_name.endswith('.json')):
        await update.message.reply_text("❌ Please upload .m3u or .json file only!")
        return
    
    await update.message.reply_text("⏳ Processing file...")
    
    try:
        file_obj = await context.bot.get_file(file.file_id)
        file_content = await file_obj.download_as_bytearray()
        content = file_content.decode('utf-8')
        
        if file_name.endswith('.m3u'):
            parse_m3u_playlist(content)
            await auto_categorize_channels()
            bot_stats['last_updated'] = datetime.now()
            await update.message.reply_text(
                f"✅ M3U uploaded successfully!\n\n"
                f"📺 {len(channels_data)} channels\n"
                f"🗂 {len(categories)} categories\n"
                f"🤖 AI categorization: {'Complete' if gemini_model else 'Skipped'}"
            )
        elif file_name.endswith('.json'):
            success = parse_json_playlist(content)
            if success:
                bot_stats['last_updated'] = datetime.now()
                await update.message.reply_text(
                    f"✅ JSON uploaded successfully!\n\n"
                    f"📺 {len(channels_data)} channels\n"
                    f"🗂 {len(categories)} categories"
                )
            else:
                await update.message.reply_text("❌ Invalid JSON format!")
    
    except Exception as e:
        logger.error(f"File upload error: {e}")
        await update.message.reply_text(f"❌ Error processing file: {str(e)}")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    user_id = update.effective_user.id
    message_text = update.message.text
    
    if is_admin(user_id):
        if context.user_data.get('awaiting_url'):
            context.user_data['awaiting_url'] = False
            await update.message.reply_text("⏳ Loading playlist from URL...")
            
            content = await load_playlist_from_url(message_text)
            if content:
                if message_text.endswith('.json'):
                    success = parse_json_playlist(content)
                    if success:
                        bot_stats['last_updated'] = datetime.now()
                        await update.message.reply_text(
                            f"✅ Loaded from URL!\n\n📺 {len(channels_data)} channels\n🗂 {len(categories)} categories"
                        )
                    else:
                        await update.message.reply_text("❌ Invalid JSON!")
                else:
                    parse_m3u_playlist(content)
                    await auto_categorize_channels()
                    bot_stats['last_updated'] = datetime.now()
                    await update.message.reply_text(
                        f"✅ Loaded from URL!\n\n📺 {len(channels_data)} channels\n🗂 {len(categories)} categories"
                    )
            else:
                await update.message.reply_text("❌ Failed to load URL!")
            return
        
        elif context.user_data.get('awaiting_broadcast'):
            context.user_data['awaiting_broadcast'] = False
            success, fail = 0, 0
            
            for uid in bot_stats['total_users']:
                try:
                    await context.bot.send_message(uid, f"📢 <b>Broadcast</b>\n\n{message_text}", parse_mode='HTML')
                    success += 1
                except:
                    fail += 1
            
            await update.message.reply_text(f"✅ Broadcast done!\n\n✔️ Sent: {success}\n❌ Failed: {fail}")
            return
    
    # Search channels
    search = message_text.lower().strip()
    matching = [(cid, ch) for cid, ch in channels_data.items() if search in ch['name'].lower()]
    
    if not matching:
        await update.message.reply_text(f"❌ No channels found for '<b>{message_text}</b>'", parse_mode='HTML')
        return
    
    keyboard = []
    for cid, ch in matching[:20]:
        keyboard.append([InlineKeyboardButton(f"▶️ {ch['name']}", callback_data=f"play_{cid}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_start")])
    
    await update.message.reply_text(
        f"🔍 Found {len(matching)} channels:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main callback handler"""
    query = update.callback_query
    data = query.data
    
    if data == "back_start":
        await start(update, context)
    elif data.startswith("cat_"):
        await category_handler(update, context)
    elif data.startswith("play_"):
        await play_channel(update, context)
    elif data == "admin_panel":
        await admin_panel(update, context)
    elif data == "admin_upload_m3u":
        await query.answer()
        await query.message.edit_text(
            "📤 <b>Upload M3U File</b>\n\nSend me an .m3u file",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
    elif data == "admin_upload_json":
        await query.answer()
        await query.message.edit_text(
            "📋 <b>Upload JSON File</b>\n\nSend me a .json file with format:\n"
            "<code>[{\"name\":\"Channel\",\"url\":\"http://...\",\"logo\":\"http://...\",\"category\":\"Sports\"}]</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
    elif data == "admin_reload_url":
        await query.answer()
        context.user_data['awaiting_url'] = True
        await query.message.edit_text(
            "🔄 <b>Reload from URL</b>\n\nSend me M3U or JSON URL:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
    elif data == "admin_ai_categorize":
        await query.answer("Starting AI categorization...", show_alert=True)
        await auto_categorize_channels()
        await query.answer("✅ AI categorization complete!", show_alert=True)
        await admin_panel(update, context)
    elif data == "admin_stats":
        await query.answer()
        cat_stats = "\n".join([f"• {cat}: {len(chs)}" for cat, chs in sorted(categories.items())[:10]])
        await query.message.edit_text(
            f"📊 <b>Statistics</b>\n\n"
            f"👥 Users: {len(bot_stats['total_users'])}\n"
            f"▶️ Plays: {bot_stats['total_plays']}\n"
            f"📺 Channels: {len(channels_data)}\n"
            f"🗂 Categories: {len(categories)}\n\n"
            f"<b>Top Categories:</b>\n{cat_stats}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
    elif data == "admin_settings":
        await query.answer()
        await query.message.edit_text(
            f"⚙️ <b>Settings</b>\n\n"
            f"Bot Name: {bot_settings['bot_name']}\n"
            f"Gemini AI: {'✅ Active' if gemini_model else '❌ Disabled'}\n"
            f"Maintenance: {'✅ ON' if bot_settings['maintenance_mode'] else '❌ OFF'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
    elif data == "admin_broadcast":
        await query.answer()
        context.user_data['awaiting_broadcast'] = True
        await query.message.edit_text(
            "📢 <b>Broadcast Message</b>\n\nSend the message to broadcast:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
    elif data == "admin_maintenance":
        bot_settings['maintenance_mode'] = not bot_settings['maintenance_mode']
        status = "enabled" if bot_settings['maintenance_mode'] else "disabled"
        await query.answer(f"🔧 Maintenance {status}!", show_alert=True)
        await admin_panel(update, context)

def main():
    """Main function"""
    # Start Flask in separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start Telegram bot
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("🚀 Bot started successfully!")
    logger.info(f"📡 Web server running on port {PORT}")
    logger.info(f"🤖 Gemini AI: {'Active' if gemini_model else 'Not configured'}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
