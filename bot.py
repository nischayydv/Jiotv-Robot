import os
import logging
import asyncio
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx
from ipytv import playlist
from ipytv.playlist import IPTVAttr

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
    raise ValueError("BOT_TOKEN required!")

# Data Store
class DataStore:
    def __init__(self):
        self.m3u_url = M3U_URL
        self.channels = {}
        self.categories = {}
        self.last_update = 0
        self.loading = False
        self.playlist_obj = None
    
    async def fetch_m3u(self, url, timeout=20):
        """Fetch M3U with httpx"""
        try:
            logger.info(f"📡 Fetching M3U...")
            
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = response.text
                
                if not content.strip():
                    logger.error("❌ Empty response")
                    return None
                
                logger.info(f"✅ Fetched {len(content)} bytes")
                return content
                
        except httpx.TimeoutException:
            logger.error(f"⏱️ Timeout after {timeout}s")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"❌ Error: {type(e).__name__}")
            return None
    
    async def parse_m3u(self):
        """Parse M3U with ipytv library"""
        if self.loading:
            logger.info("⏳ Already loading")
            return False
        
        self.loading = True
        
        try:
            if not self.m3u_url:
                logger.error("❌ No M3U URL")
                return False
            
            # Fetch content
            content = await self.fetch_m3u(self.m3u_url)
            
            if not content:
                logger.error("❌ Failed to fetch")
                return False
            
            # Save temporarily
            temp_file = '/tmp/bot_playlist.m3u'
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Parse with ipytv
            logger.info("🔄 Parsing with IPyTV...")
            
            # Run blocking I/O in executor
            loop = asyncio.get_event_loop()
            m3u_playlist = await loop.run_in_executor(None, playlist.loadf, temp_file)
            
            if not m3u_playlist or len(m3u_playlist) == 0:
                logger.error("❌ No channels")
                return False
            
            self.playlist_obj = m3u_playlist
            
            # Convert
            channels = {}
            categories = {}
            
            for idx, channel in enumerate(m3u_playlist):
                name = channel.name or f'Channel {idx + 1}'
                logo = channel.attributes.get(IPTVAttr.TVG_LOGO.value, '')
                category = channel.attributes.get(IPTVAttr.GROUP_TITLE.value, 'Uncategorized')
                tvg_id = channel.attributes.get(IPTVAttr.TVG_ID.value, '')
                url = channel.url or ''
                
                if not url:
                    continue
                
                channel_data = {
                    'name': name,
                    'logo': logo,
                    'category': category,
                    'tvg_id': tvg_id,
                    'url': url
                }
                
                if category not in categories:
                    categories[category] = []
                categories[category].append(channel_data.copy())
                
                channel_id = len(channels)
                channels[channel_id] = channel_data
            
            if channels:
                self.channels = channels
                self.categories = categories
                self.last_update = time.time()
                
                logger.info(f"✅ {len(channels)} channels, {len(categories)} categories")
                
                # Top 3 categories
                top_cats = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)[:3]
                for cat, ch_list in top_cats:
                    logger.info(f"  📂 {cat}: {len(ch_list)}")
                
                return True
            else:
                logger.error("❌ No valid channels")
                return False
                
        except Exception as e:
            logger.error(f"❌ Parse error: {e}")
            return False
        finally:
            self.loading = False
    
    def search_channels(self, query):
        """Search channels"""
        query = query.lower()
        results = []
        for channel_id, channel in self.channels.items():
            if query in channel['name'].lower():
                results.append((channel_id, channel))
        return results[:10]

# Initialize
data_store = DataStore()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start"""
    user = update.effective_user
    
    # Load if needed
    if not data_store.categories:
        msg = await update.message.reply_text("⏳ Loading channels...")
        if await data_store.parse_m3u():
            await msg.edit_text("✅ Ready!")
            await asyncio.sleep(1)
        else:
            await msg.edit_text(
                "❌ **Failed to load**\n\n"
                "Admin can configure via panel."
            )
            keyboard = []
            if user.id in ADMIN_IDS:
                keyboard.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin_panel")])
            if keyboard:
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Configure:", reply_markup=reply_markup)
            return
    
    keyboard = []
    
    # Categories (2 per row)
    categories = sorted(data_store.categories.keys())
    for i in range(0, len(categories), 2):
        row = []
        for cat in categories[i:i+2]:
            count = len(data_store.categories[cat])
            cat_name = cat[:15] + '...' if len(cat) > 15 else cat
            row.append(InlineKeyboardButton(
                f"📺 {cat_name} ({count})",
                callback_data=f"cat_{cat[:50]}"
            ))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔍 Search", switch_inline_query_current_chat="")])
    
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
🎬 **Jio TV Bot**

Hello {user.first_name}!

📺 {len(data_store.channels)} channels
📂 {len(data_store.categories)} categories

Select category or search:

Credits - @NY_BOTS
"""
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Category selection"""
    query = update.callback_query
    await query.answer()
    
    category = query.data.replace('cat_', '')
    
    if category not in data_store.categories:
        await query.message.edit_text("❌ Not found!")
        return
    
    channels = data_store.categories[category]
    keyboard = []
    
    for i in range(0, len(channels), 2):
        row = []
        for j in range(i, min(i+2, len(channels))):
            channel = channels[j]
            ch_id = next((cid for cid, ch in data_store.channels.items() 
                         if ch['name'] == channel['name'] and ch['url'] == channel['url']), None)
            if ch_id is not None:
                name = channel['name'][:25] + '...' if len(channel['name']) > 25 else channel['name']
                row.append(InlineKeyboardButton(name, callback_data=f"play_{ch_id}"))
        if row:
            keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(
        f"📺 **{category}**\n\n({len(channels)} channels)",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def play_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Play channel"""
    query = update.callback_query
    await query.answer()
    
    try:
        ch_id = int(query.data.replace('play_', ''))
    except ValueError:
        await query.message.edit_text("❌ Invalid!")
        return
    
    if ch_id not in data_store.channels:
        await query.message.edit_text("❌ Not found!")
        return
    
    channel = data_store.channels[ch_id]
    player_url = f"{WEB_APP_URL}/player?ch={ch_id}"
    
    keyboard = [
        [InlineKeyboardButton("▶️ Watch Now", web_app=WebAppInfo(url=player_url))],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"cat_{channel['category'][:50]}")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
🎬 **{channel['name']}**

📂 {channel['category']}

Click "Watch Now" to stream!

Credits - @NY_BOTS
"""
    
    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel"""
    query = update.callback_query
    user = query.from_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("⛔ Denied!", show_alert=True)
        return
    
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🔗 Change URL", callback_data="admin_change_m3u")],
        [InlineKeyboardButton("🔄 Reload", callback_data="admin_reload")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🧪 Test", callback_data="admin_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    url_display = data_store.m3u_url[:35] + '...' if len(data_store.m3u_url) > 35 else data_store.m3u_url
    
    text = f"""
⚙️ **Admin**

**URL:** `{url_display or "Not set"}`

📺 {len(data_store.channels)} channels
📂 {len(data_store.categories)} categories
"""
    
    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_change_m3u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change M3U"""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text(
        "🔗 **Change M3U URL**\n\n"
        "Send new URL:\n"
        "• https://kliv.fun/Tp7\n"
        "• https://your-m3u-url.com\n\n"
        "/cancel to abort",
        parse_mode='Markdown'
    )
    
    context.user_data['awaiting_m3u'] = True

async def admin_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload"""
    query = update.callback_query
    await query.answer("🔄 Reloading...")
    
    await query.message.edit_text("⏳ Please wait...")
    
    if await data_store.parse_m3u():
        await query.message.edit_text(
            f"✅ **Success!**\n\n"
            f"📺 {len(data_store.channels)} channels\n"
            f"📂 {len(data_store.categories)} categories",
            parse_mode='Markdown'
        )
    else:
        await query.message.edit_text("❌ **Failed!**\n\nCheck URL and retry.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stats"""
    query = update.callback_query
    await query.answer()
    
    text = f"📊 **Stats**\n\n📺 Total: {len(data_store.channels)}\n📂 Categories: {len(data_store.categories)}\n\n"
    
    for cat, chs in sorted(data_store.categories.items(), key=lambda x: len(x[1]), reverse=True)[:8]:
        text += f"• {cat}: {len(chs)}\n"
    
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test URL"""
    query = update.callback_query
    await query.answer()
    
    if not data_store.m3u_url:
        await query.message.edit_text("❌ No URL!")
        return
    
    await query.message.edit_text("🧪 Testing...")
    
    content = await data_store.fetch_m3u(data_store.m3u_url)
    
    if content:
        lines = content.split('\n')
        preview = '\n'.join(lines[:4])
        
        text = f"""
✅ **Test OK!**

Size: {len(content)} bytes
Lines: {len(lines)}

**Preview:**
```
{preview[:150]}
```
"""
        keyboard = [[InlineKeyboardButton("🔄 Reload", callback_data="admin_reload")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await query.message.edit_text("❌ **Failed!**\n\nURL not accessible.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text messages"""
    user = update.effective_user
    text = update.message.text
    
    # M3U URL update
    if context.user_data.get('awaiting_m3u') and user.id in ADMIN_IDS:
        if text == '/cancel':
            context.user_data['awaiting_m3u'] = False
            await update.message.reply_text("❌ Cancelled")
            return
        
        if text.startswith(('http://', 'https://')):
            data_store.m3u_url = text
            context.user_data['awaiting_m3u'] = False
            
            msg = await update.message.reply_text("⏳ Loading...")
            
            if await data_store.parse_m3u():
                await msg.edit_text(
                    f"✅ **Updated!**\n\n📺 {len(data_store.channels)} channels\n📂 {len(data_store.categories)} categories",
                    parse_mode='Markdown'
                )
            else:
                await msg.edit_text("❌ **Failed!**\n\nCheck URL format.")
        else:
            await update.message.reply_text("❌ Invalid URL!")
        return
    
    # Search
    if not data_store.channels:
        await update.message.reply_text("⚠️ No channels loaded yet. Use /start")
        return
    
    results = data_store.search_channels(text)
    
    if not results:
        await update.message.reply_text(
            f"❌ No results for: *{text}*\n\n💡 Try different keywords",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for ch_id, channel in results:
        name = channel['name'][:30] + '...' if len(channel['name']) > 30 else channel['name']
        keyboard.append([InlineKeyboardButton(
            f"📺 {name}",
            callback_data=f"play_{ch_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🔍 **Search Results**\n\nFound {len(results)} channels for: *{text}*",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    data = query.data
    
    try:
        if data.startswith('cat_'):
            await category_handler(update, context)
        elif data.startswith('play_'):
            await play_handler(update, context)
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
        elif data == 'back_to_start':
            await start(update, context)
        else:
            await query.answer("Unknown action")
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await query.answer("❌ Error occurred", show_alert=True)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Main function"""
    logger.info("🚀 Starting bot...")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # Start bot
    logger.info("✅ Bot is running!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()'name']) >
