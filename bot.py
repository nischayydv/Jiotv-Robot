import os
import logging
import asyncio
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import aiohttp
import re

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '').split(','))) if os.environ.get('ADMIN_IDS') else []
WEB_APP_URL = os.environ.get('WEB_APP_URL', 'https://your-render-app.onrender.com')
M3U_URL = os.environ.get('M3U_URL', '')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

# In-memory storage
class DataStore:
    def __init__(self):
        self.m3u_url = M3U_URL
        self.channels = {}
        self.categories = {}
        self.last_update = 0
        self.loading = False
    
    async def fetch_m3u_content(self, url, max_retries=3):
        """Async fetch M3U content with retry logic"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache'
        }
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Fetching M3U from {url} (Attempt {attempt + 1}/{max_retries})")
                
                timeout = aiohttp.ClientTimeout(total=30, connect=10)
                async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                    async with session.get(url, allow_redirects=True) as response:
                        response.raise_for_status()
                        content = await response.text()
                        
                        if not content.strip():
                            logger.error("Empty response received")
                            continue
                        
                        logger.info(f"Successfully fetched {len(content)} bytes")
                        return content
                        
            except asyncio.TimeoutError:
                logger.error(f"Timeout error (attempt {attempt + 1})")
                await asyncio.sleep(2 ** attempt)
                
            except aiohttp.ClientError as e:
                logger.error(f"Client error: {type(e).__name__}: {e} (attempt {attempt + 1})")
                await asyncio.sleep(2 ** attempt)
                
            except Exception as e:
                logger.error(f"Unexpected error: {type(e).__name__}: {e}")
                await asyncio.sleep(2 ** attempt)
        
        logger.error(f"Failed to fetch M3U after {max_retries} attempts")
        return None
    
    async def parse_m3u(self):
        """Parse M3U playlist with support for multiple formats"""
        if self.loading:
            logger.info("Already loading playlist, skipping")
            return False
        
        self.loading = True
        
        try:
            if not self.m3u_url:
                logger.error("No M3U URL configured")
                return False
            
            content = await self.fetch_m3u_content(self.m3u_url)
            
            if not content:
                logger.error("Failed to fetch M3U content")
                return False
            
            channels = {}
            categories = {}
            
            lines = content.split('\n')
            current_channel = {}
            line_count = 0
            
            for i, line in enumerate(lines):
                line = line.strip()
                
                # Skip empty lines and comments
                if not line or line.startswith('##'):
                    continue
                
                # Handle #EXTINF lines (standard M3U format)
                if line.startswith('#EXTINF:') or line.startswith('EXTINF:'):
                    line_count += 1
                    
                    # Extract tvg-logo
                    logo_match = re.search(r'tvg-logo="([^"]*)"', line)
                    logo = logo_match.group(1) if logo_match else ''
                    
                    # Extract group-title (category)
                    category_match = re.search(r'group-title="([^"]*)"', line)
                    category = category_match.group(1) if category_match else 'Uncategorized'
                    
                    # Extract tvg-id
                    tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
                    tvg_id = tvg_id_match.group(1) if tvg_id_match else ''
                    
                    # Extract tvg-name
                    tvg_name_match = re.search(r'tvg-name="([^"]*)"', line)
                    tvg_name = tvg_name_match.group(1) if tvg_name_match else ''
                    
                    # Extract channel name (after last comma)
                    name_match = re.search(r',(.+)$', line)
                    if name_match:
                        name = name_match.group(1).strip()
                    else:
                        # Fallback: use tvg-name, tvg-id or generate name
                        name = tvg_name or tvg_id or f'Channel {line_count}'
                    
                    current_channel = {
                        'name': name,
                        'logo': logo,
                        'category': category,
                        'tvg_id': tvg_id,
                        'url': ''
                    }
                
                # Handle stream URLs
                elif line and not line.startswith('#') and current_channel:
                    # This is the stream URL
                    stream_url = line.strip()
                    
                    # Validate URL format
                    if stream_url.startswith(('http://', 'https://', 'rtmp://', 'rtsp://')):
                        current_channel['url'] = stream_url
                        
                        # Add to categories
                        if current_channel['category'] not in categories:
                            categories[current_channel['category']] = []
                        
                        categories[current_channel['category']].append(current_channel.copy())
                        
                        # Add to channels dict
                        channel_id = len(channels)
                        channels[channel_id] = current_channel.copy()
                        
                        current_channel = {}
                
                # Handle plain URLs (no #EXTINF - fallback mode)
                elif line.startswith(('http://', 'https://')) and not current_channel:
                    line_count += 1
                    # Create a basic channel entry
                    channel = {
                        'name': f'Channel {line_count}',
                        'logo': '',
                        'category': 'Uncategorized',
                        'tvg_id': '',
                        'url': line.strip()
                    }
                    
                    if channel['category'] not in categories:
                        categories[channel['category']] = []
                    
                    categories[channel['category']].append(channel.copy())
                    
                    channel_id = len(channels)
                    channels[channel_id] = channel.copy()
            
            if channels:
                self.channels = channels
                self.categories = categories
                self.last_update = time.time()
                logger.info(f"✅ Successfully parsed {len(channels)} channels in {len(categories)} categories")
                
                # Log category breakdown
                for cat, ch_list in sorted(categories.items(), key=lambda x: len(x[1]), reverse=True):
                    logger.info(f"  📂 {cat}: {len(ch_list)} channels")
                
                return True
            else:
                logger.error("❌ No channels found in M3U playlist")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error parsing M3U: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
        finally:
            self.loading = False
    
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
        msg = await update.message.reply_text("⏳ Loading channels... Please wait.")
        if await data_store.parse_m3u():
            await msg.edit_text("✅ Channels loaded successfully!")
            await asyncio.sleep(1)
        else:
            await msg.edit_text(
                "❌ **Failed to load channels.**\n\n"
                "**Possible reasons:**\n"
                "• M3U URL is not accessible\n"
                "• Network timeout\n"
                "• Invalid M3U format\n\n"
                "Please contact admin or try again later.",
                parse_mode='Markdown'
            )
            return
    
    keyboard = []
    
    # Create category buttons (2 per row for better mobile UX)
    categories = sorted(data_store.categories.keys())
    for i in range(0, len(categories), 2):
        row = []
        for cat in categories[i:i+2]:
            count = len(data_store.categories[cat])
            # Truncate category name if too long
            cat_display = cat[:20] + '...' if len(cat) > 20 else cat
            row.append(InlineKeyboardButton(
                f"📺 {cat_display} ({count})",
                callback_data=f"cat_{cat[:50]}"  # Limit callback data length
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

💡 **Tip:** You can also search for channels by typing the channel name!

📱 **Features:**
• HD Quality Streaming
• Mini App Player
• Secure URLs
• Mobile Optimized

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
                             if ch['name'] == channel['name'] and ch['url'] == channel['url']), None)
            if channel_id is not None:
                # Truncate channel name if too long
                display_name = channel['name'][:30] + '...' if len(channel['name']) > 30 else channel['name']
                row.append(InlineKeyboardButton(
                    display_name,
                    callback_data=f"play_{channel_id}"
                ))
        if row:  # Only add non-empty rows
            keyboard.append(row)
    
    # Add back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Categories", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(
        f"📺 **{category}**\n\n"
        f"🎬 Select a channel to watch:\n\n"
        f"({len(channels)} channels available)",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def play_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel play request"""
    query = update.callback_query
    await query.answer()
    
    try:
        channel_id = int(query.data.replace('play_', ''))
    except ValueError:
        await query.message.edit_text("❌ Invalid channel!")
        return
    
    if channel_id not in data_store.channels:
        await query.message.edit_text("❌ Channel not found!")
        return
    
    channel = data_store.channels[channel_id]
    
    # Create Mini App URL with channel ID
    player_url = f"{WEB_APP_URL}/player?ch={channel_id}"
    
    keyboard = [
        [InlineKeyboardButton(
            "▶️ Watch Now",
            web_app=WebAppInfo(url=player_url)
        )],
        [InlineKeyboardButton(
            "⬅️ Back",
            callback_data=f"cat_{channel['category'][:50]}"
        )]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    channel_info = f"""
🎬 **{channel['name']}**

📂 Category: {channel['category']}

✨ Click "▶️ Watch Now" to start streaming!

🔒 Secure streaming via Mini App
📱 Works on all devices
🎥 HD Quality

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
        [InlineKeyboardButton("🧪 Test M3U URL", callback_data="admin_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    url_display = data_store.m3u_url[:60] + '...' if len(data_store.m3u_url) > 60 else data_store.m3u_url
    
    admin_text = f"""
⚙️ **Admin Panel**

**Current M3U URL:**
`{url_display if data_store.m3u_url else "Not set"}`

**Statistics:**
📺 Channels: {len(data_store.channels)}
📂 Categories: {len(data_store.categories)}
🕐 Last Update: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data_store.last_update)) if data_store.last_update else 'Never'}

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
        "🔗 **Change M3U URL**\n\n"
        "Please send the new M3U playlist URL:\n\n"
        "**Supported formats:**\n"
        "• `https://example.com/playlist.m3u`\n"
        "• `https://example.com/playlist.m3u8`\n"
        "• `https://example.com/playlist.php`\n"
        "• `https://kliv.fun/Tp7`\n"
        "• `https://public.kliv.fun/Mac21o/playlist.php`\n\n"
        "Send /cancel to abort.",
        parse_mode='Markdown'
    )
    
    context.user_data['awaiting_m3u'] = True

async def admin_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload M3U playlist"""
    query = update.callback_query
    await query.answer("🔄 Reloading playlist...")
    
    await query.message.edit_text("⏳ Reloading playlist... Please wait.")
    
    if await data_store.parse_m3u():
        await query.message.edit_text(
            f"✅ **Playlist Reloaded Successfully!**\n\n"
            f"📺 Channels: {len(data_store.channels)}\n"
            f"📂 Categories: {len(data_store.categories)}\n"
            f"🕐 Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data_store.last_update))}",
            parse_mode='Markdown'
        )
    else:
        await query.message.edit_text(
            "❌ **Failed to reload playlist!**\n\n"
            "**Please check:**\n"
            "• M3U URL is accessible\n"
            "• URL format is correct\n"
            "• Internet connection is stable\n"
            "• Server is not rate-limiting\n\n"
            "Try the 'Test M3U URL' option first."
        )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics"""
    query = update.callback_query
    await query.answer()
    
    stats_text = "📊 **Detailed Statistics**\n\n"
    stats_text += f"📺 Total Channels: {len(data_store.channels)}\n"
    stats_text += f"📂 Total Categories: {len(data_store.categories)}\n\n"
    stats_text += "**Channels per Category:**\n"
    
    # Sort by channel count (descending)
    for cat, channels in sorted(data_store.categories.items(), key=lambda x: len(x[1]), reverse=True):
        stats_text += f"• {cat}: {len(channels)} channels\n"
    
    # Add top 5 categories info
    stats_text += f"\n🔝 **Top Category:** {max(data_store.categories.items(), key=lambda x: len(x[1]))[0] if data_store.categories else 'N/A'}"
    
    keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(
        stats_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def admin_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test current M3U URL"""
    query = update.callback_query
    await query.answer()
    
    if not data_store.m3u_url:
        await query.message.edit_text("❌ No M3U URL configured!")
        return
    
    await query.message.edit_text("🧪 Testing M3U URL... Please wait.")
    
    content = await data_store.fetch_m3u_content(data_store.m3u_url)
    
    if content:
        lines = content.split('\n')
        total_lines = len(lines)
        
        # Count channels
        channel_count = sum(1 for line in lines if line.strip().startswith('#EXTINF'))
        
        # Get preview
        preview_lines = lines[:10]
        preview = '\n'.join(preview_lines)
        
        result_text = f"""
✅ **M3U URL Test: Success**

**URL:** `{data_store.m3u_url[:60]}...`
**Size:** {len(content)} bytes
**Total Lines:** {total_lines}
**Detected Channels:** {channel_count}

**Preview (first 10 lines):**
```
{preview[:500]}
```

✅ The URL is accessible and returning data!
"""
        keyboard = [
            [InlineKeyboardButton("🔄 Reload with this URL", callback_data="admin_reload")],
            [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            result_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        result_text = f"""
❌ **M3U URL Test: Failed**

**URL:** `{data_store.m3u_url}`

**Possible issues:**
• URL is not accessible
• Server is down or timing out
• Network connection issues
• Authentication required
• Server is rate-limiting requests

**Try these solutions:**
1. Check if URL works in browser
2. Wait 5-10 minutes and try again
3. Try a different M3U URL
4. Contact M3U provider

Use "Change M3U URL" to try a different source.
"""
        keyboard = [
            [InlineKeyboardButton("🔗 Change M3U URL", callback_data="admin_change_m3u")],
            [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            result_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (channel search and M3U URL update)"""
    user = update.effective_user
    text = update.message.text
    
    # Check if admin is setting M3U URL
    if context.user_data.get('awaiting_m3u') and user.id in ADMIN_IDS:
        if text == '/cancel':
            context.user_data['awaiting_m3u'] = False
            await update.message.reply_text("❌ Cancelled.")
            return
        
        if text.startswith(('http://', 'https://')):
            data_store.m3u_url = text
            context.user_data['awaiting_m3u'] = False
            
            msg = await update.message.reply_text("⏳ Loading new playlist... Please wait.")
            
            if await data_store.parse_m3u():
                await msg.edit_text(
                    f"✅ **M3U URL Updated Successfully!**\n\n"
                    f"📺 Channels: {len(data_store.channels)}\n"
                    f"📂 Categories: {len(data_store.categories)}\n"
                    f"🕐 Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data_store.last_update))}",
                    parse_mode='Markdown'
                )
            else:
                await msg.edit_text(
                    "❌ **Failed to parse M3U playlist!**\n\n"
                    "**Possible reasons:**\n"
                    "• Invalid M3U format\n"
                    "• No channels in playlist\n"
                    "• URL not accessible\n\n"
                    "Please check the URL and try again."
                )
        else:
            await update.message.reply_text(
                "❌ Invalid URL!\n\n"
                "Please send a valid HTTP/HTTPS URL.\n"
                "Example: `https://example.com/playlist.m3u`",
                parse_mode='Markdown'
            )
        return
    
    # Search for channels
    if not data_store.channels:
        await update.message.reply_text(
            "⚠️ No channels loaded yet.\n\n"
            "Please use /start to load channels first."
        )
        return
    
    results = data_store.search_channels(text)
    
    if not results:
        await update.message.reply_text(
            f"❌ No channels found for: *{text}*\n\n"
            f"💡 **Tips:**\n"
            f"• Try different keywords\n"
            f"• Check spelling\n"
            f"• Browse by category instead",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for channel_id, channel in results:
        display_name = channel['name'][:35] + '...' if len(channel['name']) > 35 else channel['name']
        keyboard.append([InlineKeyboardButton(
            f"📺 {display_name}",
            callback_data=f"play_{channel_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Categories", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🔍 **Search Results for:** `{text}`\n\n"
        f"Found {len(results)} channel(s):",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callback queries"""
    query = update.callback_query
    data = query.data
    
    try:
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
        elif data == 'admin_test':
            await admin_test(update, context)
        else:
            await query.answer("❌ Unknown action", show_alert=True)
    except Exception as e:
        logger.error(f"Error in callback router: {type(e).__name__}: {e}")
        await query.answer("⚠️ An error occurred. Please try again.", show_alert=True)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An error occurred while processing your request.\n\n"
                "Please try again or contact admin if the issue persists."
            )
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")

async def post_init(application: Application):
    """Post initialization - load channels on startup"""
    logger.info("🤖 Bot initialized successfully!")
    logger.info(f"🌐 Web App URL: {WEB_APP_URL}")
    logger.info(f"👥 Admin IDs: {ADMIN_IDS}")
    logger.info(f"🔗 M3U URL: {data_store.m3u_url[:60]}..." if data_store.m3u_url else "No M3U URL configured")
    
    if data_store.m3u_url:
        logger.info("📡 Loading channels from M3U...")
        success = await data_store.parse_m3u()
        if success:
            logger.info("✅ Channels loaded successfully on startup")
        else:
            logger.error("❌ Failed to load channels on startup - bot will continue without channels")
    else:
        logger.warning("⚠️ No M3U_URL configured - bot started without channels")

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not provided! Set BOT_TOKEN environment variable.")
        return
    
    logger.info("🚀 Starting Jio TV Bot...")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Start bot
    logger.info("✅ Bot handlers registered")
    logger.info("🎬 Starting polling...")
    
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, 
        drop_pending_updates=True,
        close_loop=False
    )

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Bot stopped manually.")
    except Exception as e:
        logger.error(f"❌ Fatal error: {type(e).__name__}: {e}")
