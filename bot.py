import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
from urllib.parse import quote

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '').split(','))) if os.environ.get('ADMIN_IDS') else []
WEB_APP_URL = os.environ.get('WEB_APP_URL', 'https://your-render-app.onrender.com')
M3U_URL = os.environ.get('M3U_URL', '')

# Create a session with retry strategy
def create_session():
    """Create a requests session with retry logic and proper headers"""
    session = requests.Session()
    
    # Retry strategy
    retry_strategy = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Set proper headers to avoid blocking
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache'
    })
    
    return session

# In-memory storage (use database in production)
class DataStore:
    def __init__(self):
        self.m3u_url = M3U_URL
        self.channels = {}
        self.categories = {}
        self.session = create_session()
    
    def parse_m3u(self):
        """Parse M3U playlist and organize by categories"""
        if not self.m3u_url:
            logger.warning("M3U URL is empty or not set")
            return False
        
        try:
            logger.info(f"Attempting to fetch M3U from: {self.m3u_url[:50]}...")
            
            # Fetch M3U with increased timeout and proper error handling
            response = self.session.get(
                self.m3u_url, 
                timeout=(30, 60),  # (connection timeout, read timeout)
                stream=True,
                allow_redirects=True
            )
            response.raise_for_status()
            
            # Get content with encoding handling
            if response.encoding is None:
                response.encoding = 'utf-8'
            
            content = response.text
            
            if not content or len(content) < 10:
                logger.error("M3U content is empty or too short")
                return False
            
            self.channels = {}
            self.categories = {}
            
            lines = content.split('
')
            current_channel = {}
            
            logger.info(f"Parsing {len(lines)} lines from M3U")
            
            for i, line in enumerate(lines):
                line = line.strip()
                
                if line.startswith('#EXTINF:'):
                    # Extract channel info
                    match = re.search(r'tvg-logo="([^"]*)"', line)
                    logo = match.group(1) if match else ''
                    
                    match = re.search(r'group-title="([^"]*)"', line)
                    category = match.group(1) if match else 'Uncategorized'
                    
                    # Extract channel name (after last comma)
                    name_match = re.search(r',(.+)$', line)
                    name = name_match.group(1).strip() if name_match else f'Channel {i}'
                    
                    current_channel = {
                        'name': name,
                        'logo': logo,
                        'category': category,
                        'url': ''
                    }
                
                elif line and not line.startswith('#') and current_channel:
                    # This is the stream URL
                    current_channel['url'] = line
                    
                    # Add to categories
                    if current_channel['category'] not in self.categories:
                        self.categories[current_channel['category']] = []
                    
                    self.categories[current_channel['category']].append(current_channel.copy())
                    
                    # Add to channels dict
                    channel_id = len(self.channels)
                    self.channels[channel_id] = current_channel.copy()
                    current_channel = {}
            
            logger.info(f"✅ Successfully parsed {len(self.channels)} channels in {len(self.categories)} categories")
            return True
        
        except requests.exceptions.Timeout:
            logger.error("❌ Timeout error: M3U URL took too long to respond")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Connection error: {str(e)}")
            logger.error("Possible causes: Invalid URL, Server blocking requests, Network issues")
            return False
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ HTTP error: {e.response.status_code} - {str(e)}")
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error parsing M3U: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def search_channels(self, query):
        """Search channels by name"""
        query = query.lower()
        results = []
        for channel_id, channel in self.channels.items():
            if query in channel['name'].lower():
                results.append((channel_id, channel))
        return results[:10]  # Limit to 10 results

# Initialize data store
data_store = DataStore()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    
    # Parse M3U if not already parsed
    if not data_store.categories:
        if not data_store.m3u_url:
            await update.message.reply_text(
                "⚠️ **M3U URL not configured!**

"
                "Please contact admin to set up the playlist URL.

"
                "Admin: Use /start and go to Admin Panel → Change M3U URL",
                parse_mode='Markdown'
            )
            return
        
        await update.message.reply_text("⏳ Loading channels... Please wait (this may take up to 60 seconds)...")
        
        if data_store.parse_m3u():
            await update.message.reply_text("✅ Channels loaded successfully!")
        else:
            await update.message.reply_text(
                "❌ **Failed to load channels!**

"
                "**Possible reasons:**
"
                "• M3U URL is invalid or unreachable
"
                "• Server is blocking requests
"
                "• Network timeout
"
                "• M3U file is too large

"
                "**Solutions:**
"
                "• Verify the M3U URL is correct
"
                "• Try again in a few minutes
"
                "• Contact your IPTV provider
"
                "• Contact bot admin for assistance

"
                f"Current URL: `{data_store.m3u_url[:50]}...`",
                parse_mode='Markdown'
            )
            return
    
    keyboard = []
    
    # Create category buttons (3 per row)
    categories = sorted(data_store.categories.keys())
    for i in range(0, len(categories), 3):
        row = []
        for cat in categories[i:i+3]:
            count = len(data_store.categories[cat])
            row.append(InlineKeyboardButton(
                f"📺 {cat} ({count})",
                callback_data=f"cat_{cat}"[:64]  # Telegram callback data limit
            ))
        keyboard.append(row)
    
    # Add search button
    keyboard.append([InlineKeyboardButton("🔍 Search Channel", switch_inline_query_current_chat="")])
    
    # Add admin panel for admins
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = f"""
🎬 **Welcome to Jio TV Bot!** 🎬

Hello {user.first_name}! 

📺 **Total Channels:** {len(data_store.channels)}
📂 **Categories:** {len(data_store.categories)}

**Select a category below to browse channels:**
👇 Choose from the buttons below 👇

💡 **Tip:** You can also search for channels by sending the channel name!

Credits - @NY_BOTS
"""
    
    if update.message:
        await update.message.reply_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.callback_query.message.edit_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection"""
    query = update.callback_query
    await query.answer()
    
    category = query.data.replace('cat_', '')
    
    if category not in data_store.categories:
        await query.message.edit_text("❌ Category not found!")
        return
    
    channels = data_store.categories[category]
    keyboard = []
    
    # Create channel buttons (2 per row)
    for i in range(0, len(channels), 2):
        row = []
        for j in range(i, min(i+2, len(channels))):
            channel = channels[j]
            # Find channel ID
            channel_id = next((cid for cid, ch in data_store.channels.items() 
                             if ch['name'] == channel['name']), None)
            if channel_id is not None:
                row.append(InlineKeyboardButton(
                    channel['name'][:30],  # Truncate long names
                    callback_data=f"play_{channel_id}"
                ))
        if row:
            keyboard.append(row)
    
    # Add back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Categories", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(
        f"📺 **{category}**

🎬 Select a channel to watch:

({len(channels)} channels available)",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def play_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel play request"""
    query = update.callback_query
    await query.answer()
    
    channel_id = int(query.data.replace('play_', ''))
    
    if channel_id not in data_store.channels:
        await query.message.edit_text("❌ Channel not found!")
        return
    
    channel = data_store.channels[channel_id]
    
    # Create Mini App URL with encrypted channel info
    player_url = f"{WEB_APP_URL}/player?ch={channel_id}"
    
    keyboard = [
        [InlineKeyboardButton(
            "▶️ Watch Now",
            web_app=WebAppInfo(url=player_url)
        )],
        [InlineKeyboardButton(
            "⬅️ Back",
            callback_data=f"cat_{channel['category']}"[:64]
        )]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    channel_info = f"""
🎬 **{channel['name']}**

📂 Category: {channel['category']}

✨ Click "▶️ Watch Now" to start streaming!

🔒 Secure streaming via Mini App
📱 Works on all devices

Credits - @NY_BOTS
"""
    
    await query.message.edit_text(
        channel_info,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin panel"""
    query = update.callback_query
    user = query.from_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("⛔ Access Denied!", show_alert=True)
        return
    
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🔗 Change M3U URL", callback_data="admin_change_m3u")],
        [InlineKeyboardButton("🔄 Reload Playlist", callback_data="admin_reload")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    url_display = data_store.m3u_url[:50] + "..." if len(data_store.m3u_url) > 50 else data_store.m3u_url
    
    admin_text = f"""
⚙️ **Admin Panel**

**Current M3U URL:**
`{url_display if data_store.m3u_url else "Not set"}`

**Statistics:**
📺 Channels: {len(data_store.channels)}
📂 Categories: {len(data_store.categories)}

**Select an option below:**
"""
    
    await query.message.edit_text(
        admin_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def admin_change_m3u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Request new M3U URL from admin"""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text(
        "🔗 **Change M3U URL**

"
        "Please send the new M3U playlist URL:

"
        "Format: `https://example.com/playlist.m3u`

"
        "Send /cancel to abort.",
        parse_mode='Markdown'
    )
    
    context.user_data['awaiting_m3u'] = True

async def admin_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload M3U playlist"""
    query = update.callback_query
    await query.answer("🔄 Reloading playlist...")
    
    await query.message.edit_text("⏳ Reloading playlist... This may take up to 60 seconds...")
    
    if data_store.parse_m3u():
        await query.message.edit_text(
            f"✅ **Playlist Reloaded Successfully!**

"
            f"📺 Channels: {len(data_store.channels)}
"
            f"📂 Categories: {len(data_store.categories)}",
            parse_mode='Markdown'
        )
    else:
        await query.message.edit_text(
            "❌ **Failed to reload playlist!**

"
            "Check logs for details. Possible issues:
"
            "• Invalid M3U URL
"
            "• Server timeout
"
            "• Network connection problem
"
            "• Server blocking requests"
        )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics"""
    query = update.callback_query
    await query.answer()
    
    stats_text = "📊 **Detailed Statistics**

"
    stats_text += f"📺 Total Channels: {len(data_store.channels)}
"
    stats_text += f"📂 Total Categories: {len(data_store.categories)}

"
    stats_text += "**Channels per Category:**
"
    
    for cat, channels in sorted(data_store.categories.items()):
        stats_text += f"• {cat}: {len(channels)} channels
"
    
    keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(
        stats_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any ongoing operation"""
    context.user_data.clear()
    await update.message.reply_text("✅ Operation cancelled.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (channel search)"""
    user = update.effective_user
    text = update.message.text
    
    # Check if admin is setting M3U URL
    if context.user_data.get('awaiting_m3u') and user.id in ADMIN_IDS:
        if text.startswith('http'):
            data_store.m3u_url = text
            context.user_data['awaiting_m3u'] = False
            await update.message.reply_text("⏳ Loading new playlist... This may take up to 60 seconds...")
            
            if data_store.parse_m3u():
                await update.message.reply_text(
                    f"✅ **M3U URL Updated Successfully!**

"
                    f"📺 Channels: {len(data_store.channels)}
"
                    f"📂 Categories: {len(data_store.categories)}",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    "❌ **Failed to parse M3U playlist!**

"
                    "Please check:
"
                    "• URL is correct and accessible
"
                    "• M3U file format is valid
"
                    "• Server is not blocking requests
"
                    "• Check bot logs for detailed error"
                )
        else:
            await update.message.reply_text("❌ Invalid URL! Please send a valid HTTP/HTTPS URL.")
        return
    
    # Search for channels
    results = data_store.search_channels(text)
    
    if not results:
        await update.message.reply_text(
            f"❌ No channels found for: *{text}*

"
            f"Try searching with different keywords!",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for channel_id, channel in results:
        keyboard.append([InlineKeyboardButton(
            f"📺 {channel['name']}",
            callback_data=f"play_{channel_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Categories", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🔍 **Search Results for:** `{text}`

"
        f"Found {len(results)} channel(s):",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callback queries"""
    query = update.callback_query
    data = query.data
    
    if data.startswith('cat_'):
        await category_handler(update, context)
    elif data.startswith('play_'):
        await play_handler(update, context)
    elif data == 'back_to_start':
        await start(update, context)
    elif data == 'admin_panel':
        await admin_panel(update, context)
    elif data == 'admin_change_m3u':
        await admin_change_m3u(update, context)
    elif data == 'admin_reload':
        await admin_reload(update, context)
    elif data == 'admin_stats':
        await admin_stats(update, context)

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Load M3U on startup
    logger.info("🚀 Starting Jio TV Bot...")
    if data_store.m3u_url:
        logger.info("📡 Loading M3U playlist on startup...")
        if data_store.parse_m3u():
            logger.info("✅ M3U playlist loaded successfully on startup!")
        else:
            logger.warning("⚠️ Failed to load M3U on startup. Will retry when user requests.")
    else:
        logger.warning("⚠️ M3U_URL environment variable not set. Bot will start without channels.")
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Start bot
    logger.info("✅ Bot started successfully!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
