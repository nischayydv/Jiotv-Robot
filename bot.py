import os
import re
import json
import logging
import asyncio
from urllib.parse import quote, urljoin, urlparse
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
from threading import Thread
import aiohttp
import requests
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure
import hashlib

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
WEBAPP_URL = os.environ.get('WEBAPP_URL', 'https://your-app.herokuapp.com')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')

# Pagination settings - 2 columns layout
CATEGORIES_PER_PAGE = 10  # 5 rows x 2 columns
CHANNELS_PER_PAGE = 10    # 5 rows x 2 columns

# MongoDB Setup
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client['tv_bot']
    channels_col = db['channels']
    categories_col = db['categories']
    stats_col = db['stats']
    sources_col = db['sources']
    
    channels_col.create_index([('id', ASCENDING)], unique=True)
    channels_col.create_index([('category', ASCENDING)])
    channels_col.create_index([('name', ASCENDING)])
    sources_col.create_index([('hash', ASCENDING)], unique=True)
    
    logger.info("✅ MongoDB connected successfully")
    MONGO_ENABLED = True
except (ConnectionFailure, Exception) as e:
    logger.warning(f"⚠️ MongoDB not available: {e}. Using in-memory storage.")
    MONGO_ENABLED = False
    channels_col = None
    categories_col = None
    stats_col = None
    sources_col = None

# Gemini AI Setup
gemini_model = None
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("✅ Gemini AI enabled")
    except Exception as e:
        logger.warning(f"⚠️ Gemini not available: {e}")

# In-memory cache
channels_cache = {}
categories_cache = {}
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

# ============= DATABASE FUNCTIONS =============

def get_all_channels():
    """Get all channels from DB or cache"""
    if MONGO_ENABLED:
        channels = list(channels_col.find({}, {'_id': 0}))
        return {ch['id']: ch for ch in channels}
    return channels_cache

def get_channel(channel_id):
    """Get single channel"""
    if MONGO_ENABLED:
        return channels_col.find_one({'id': channel_id}, {'_id': 0})
    return channels_cache.get(channel_id)

def save_channel(channel_data):
    """Save or update channel"""
    if MONGO_ENABLED:
        channels_col.update_one(
            {'id': channel_data['id']},
            {'$set': channel_data},
            upsert=True
        )
    else:
        channels_cache[channel_data['id']] = channel_data

def get_categories():
    """Get organized categories"""
    if MONGO_ENABLED:
        pipeline = [
            {'$group': {'_id': '$category', 'channels': {'$push': '$id'}, 'count': {'$sum': 1}}},
            {'$sort': {'_id': 1}}
        ]
        result = list(channels_col.aggregate(pipeline))
        return {item['_id']: item['channels'] for item in result}
    
    # Build categories from cache
    cats = {}
    for cid, ch in channels_cache.items():
        cat = ch.get('category', 'Other')
        if cat not in cats:
            cats[cat] = []
        cats[cat].append(cid)
    return cats

def get_channels_by_category(category):
    """Get channels in a category"""
    if MONGO_ENABLED:
        channels = list(channels_col.find({'category': category}, {'_id': 0}).sort('name', ASCENDING))
        return channels
    cats = get_categories()
    return [channels_cache.get(cid) for cid in cats.get(category, []) if cid in channels_cache]

def check_source_processed(content):
    """Check if this source was already processed - DISABLED for testing"""
    # Temporarily disable duplicate checking to allow re-importing
    return False
    
    if not MONGO_ENABLED:
        return False
    
    content_hash = hashlib.md5(content.encode()).hexdigest()
    return sources_col.find_one({'hash': content_hash}) is not None

def mark_source_processed(content, source_info):
    """Mark source as processed"""
    if not MONGO_ENABLED:
        return
    
    content_hash = hashlib.md5(content.encode()).hexdigest()
    sources_col.update_one(
        {'hash': content_hash},
        {'$set': {
            'hash': content_hash,
            'source': source_info,
            'processed_at': datetime.now(),
            'channel_count': len(json.loads(content)) if isinstance(content, str) else 0
        }},
        upsert=True
    )

def update_stats(stat_type, value=1):
    """Update bot statistics"""
    if MONGO_ENABLED:
        stats_col.update_one(
            {'type': stat_type},
            {'$inc': {'value': value}, '$set': {'updated_at': datetime.now()}},
            upsert=True
        )
    else:
        if stat_type == 'plays':
            bot_stats['total_plays'] += value
        elif stat_type == 'users':
            bot_stats['total_users'].add(value)

def get_stats():
    """Get bot statistics"""
    if MONGO_ENABLED:
        total_channels = channels_col.count_documents({})
        total_categories = len(get_categories())
        plays = stats_col.find_one({'type': 'plays'})
        users = stats_col.find_one({'type': 'users'})
        
        return {
            'channels': total_channels,
            'categories': total_categories,
            'plays': plays['value'] if plays else 0,
            'users': users['value'] if users else 0
        }
    else:
        return {
            'channels': len(channels_cache),
            'categories': len(get_categories()),
            'plays': bot_stats['total_plays'],
            'users': len(bot_stats['total_users'])
        }

# ============= M3U PARSING =============

def parse_m3u_content(content, base_url=''):
    """Parse M3U/M3U8 playlist content with better error handling"""
    channels = []
    lines = content.strip().split('\n')
    
    logger.info(f"📝 Parsing M3U content ({len(lines)} lines)")
    
    current_channel = {}
    
    for i, line in enumerate(lines):
        line = line.strip()
        
        # Skip empty lines and comments (except EXTINF)
        if not line or (line.startswith('#') and not line.startswith('#EXTINF')):
            continue
        
        if line.startswith('#EXTINF:'):
            # Parse channel info
            # Format: #EXTINF:-1 tvg-id="id" tvg-name="name" tvg-logo="logo" group-title="category",Channel Name
            
            # Extract attributes using regex
            tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
            tvg_name_match = re.search(r'tvg-name="([^"]*)"', line)
            tvg_logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            group_match = re.search(r'group-title="([^"]*)"', line)
            
            # Extract channel name (after last comma)
            name_match = re.search(r',(.+)$', line)
            
            # Generate unique ID
            ch_name = name_match.group(1).strip() if name_match else (tvg_name_match.group(1) if tvg_name_match else f"Channel {i}")
            ch_id = tvg_id_match.group(1) if tvg_id_match and tvg_id_match.group(1) else f"ch_{hashlib.md5(ch_name.encode()).hexdigest()[:8]}"
            
            current_channel = {
                'id': ch_id,
                'name': ch_name,
                'logo': tvg_logo_match.group(1) if tvg_logo_match else '',
                'category': group_match.group(1) if group_match else None,
            }
            
        elif line and not line.startswith('#') and current_channel:
            # This is the stream URL
            stream_url = line.strip()
            
            # Handle relative URLs
            if base_url and not stream_url.startswith(('http://', 'https://', 'rtmp://', 'rtsp://')):
                stream_url = urljoin(base_url, stream_url)
            
            current_channel['link'] = stream_url
            
            # Detect stream type and proxy requirements
            if '.m3u8' in stream_url.lower():
                current_channel['stream_type'] = 'hls'
            elif '.mpd' in stream_url.lower():
                current_channel['stream_type'] = 'dash'
            else:
                current_channel['stream_type'] = 'hls'  # Default
            
            # Detect if it needs special handling
            if 'servertvhub.site' in stream_url:
                current_channel['needs_proxy'] = True
                logger.info(f"  🔍 ServerTVHub URL detected: {stream_url}")
                
                # If it's a PHP endpoint, mark it specially
                if '.php' in stream_url:
                    current_channel['is_php_endpoint'] = True
                    logger.info(f"  ⚙️ PHP endpoint detected - will fetch actual stream")
            elif 'live.php' in stream_url or 'playlist.php' in stream_url:
                current_channel['needs_proxy'] = True
                current_channel['is_php_endpoint'] = True
            
            channels.append(current_channel.copy())
            logger.info(f"  ✓ Parsed: {current_channel['name']} ({current_channel.get('stream_type', 'unknown')})")
            current_channel = {}
    
    logger.info(f"✅ Parsed {len(channels)} channels from M3U")
    return channels

def parse_servertvhub_playlist(content, base_url):
    """Parse servertvhub.site style playlist with better error handling"""
    channels = []
    
    logger.info(f"📝 Parsing servertvhub content (length: {len(content)})")
    
    # Try to extract channel data from PHP response
    try:
        # First, try JSON format
        try:
            data = json.loads(content)
            logger.info(f"✅ Parsed as JSON, found {len(data) if isinstance(data, list) else 'unknown'} items")
            
            if isinstance(data, list):
                for idx, item in enumerate(data):
                    try:
                        channel = {
                            'id': item.get('id', f"stv_{idx}"),
                            'name': item.get('name', item.get('title', f'Channel {idx}')),
                            'logo': item.get('logo', item.get('image', '')),
                            'link': item.get('url', item.get('link', item.get('stream_url', ''))),
                            'category': item.get('category', item.get('group', None)),
                            'needs_proxy': True
                        }
                        
                        # Make sure we have at least a name and link
                        if channel['name'] and channel['link']:
                            channels.append(channel)
                            logger.info(f"  ✓ Added: {channel['name']}")
                    except Exception as e:
                        logger.error(f"  ✗ Error parsing item {idx}: {e}")
                        continue
                        
            elif isinstance(data, dict):
                # Handle dict with channels key
                if 'channels' in data:
                    for idx, item in enumerate(data['channels']):
                        try:
                            channel = {
                                'id': item.get('id', f"stv_{idx}"),
                                'name': item.get('name', item.get('title', f'Channel {idx}')),
                                'logo': item.get('logo', item.get('image', '')),
                                'link': item.get('url', item.get('link', item.get('stream_url', ''))),
                                'category': item.get('category', item.get('group', None)),
                                'needs_proxy': True
                            }
                            
                            if channel['name'] and channel['link']:
                                channels.append(channel)
                                logger.info(f"  ✓ Added: {channel['name']}")
                        except Exception as e:
                            logger.error(f"  ✗ Error parsing channel {idx}: {e}")
                            continue
                            
            return channels
            
        except json.JSONDecodeError:
            logger.info("⚠️ Not JSON format, trying M3U format")
            pass
        
        # If not JSON, try M3U format
        channels = parse_m3u_content(content, base_url)
        
        # Mark all servertvhub channels as needing proxy
        for ch in channels:
            ch['needs_proxy'] = True
        
        logger.info(f"✅ Parsed as M3U, found {len(channels)} channels")
        return channels
        
    except Exception as e:
        logger.error(f"❌ Error parsing servertvhub playlist: {e}")
        import traceback
        traceback.print_exc()
        return []

# ============= FLASK ROUTES =============

@app.route('/')
def index():
    channels = list(get_all_channels().values())
    categories = get_categories()
    return render_template('index.html', channels=channels, categories=categories)

@app.route('/player')
def player():
    channel_id = request.args.get('id', '')
    channel = get_channel(channel_id)
    
    if not channel:
        return "Channel not found", 404
    
    update_stats('plays', 1)
    return render_template('player.html', channel=channel, webapp_url=WEBAPP_URL)

@app.route('/proxy/<channel_id>')
def proxy_manifest(channel_id):
    """Proxy DASH/HLS manifest with cookies"""
    try:
        channel = get_channel(channel_id)
        if not channel:
            return jsonify({'error': 'Channel not found'}), 404
        
        manifest_url = channel.get('link', '')
        cookie = channel.get('cookie', '')
        
        if not manifest_url:
            return jsonify({'error': 'No manifest URL'}), 400
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.jiocinema.com/',
            'Origin': 'https://www.jiocinema.com'
        }
        
        if cookie:
            headers['Cookie'] = cookie
        
        response = requests.get(manifest_url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch manifest: {response.status_code}")
            return jsonify({'error': f'Manifest fetch failed: {response.status_code}'}), 502
        
        content = response.text
        content_type = 'application/dash+xml' if '.mpd' in manifest_url else 'application/vnd.apple.mpegurl'
        
        # Modify URLs to use proxy
        if channel.get('needs_proxy'):
            content = content.replace('BaseURL>', f'BaseURL>{WEBAPP_URL}/proxy-segment/{channel_id}/')
        
        return Response(
            content,
            mimetype=content_type,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
                'Cache-Control': 'no-cache'
            }
        )
        
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/proxy-segment/<channel_id>/<path:segment_path>')
def proxy_segment(channel_id, segment_path):
    """Proxy video segments with cookies"""
    try:
        channel = get_channel(channel_id)
        if not channel:
            return jsonify({'error': 'Channel not found'}), 404
        
        manifest_url = channel.get('link', '')
        cookie = channel.get('cookie', '')
        
        manifest_base = manifest_url.rsplit('/', 1)[0] + '/'
        segment_url = urljoin(manifest_base, segment_path)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.jiocinema.com/',
            'Origin': 'https://www.jiocinema.com'
        }
        
        if cookie:
            headers['Cookie'] = cookie
        
        response = requests.get(segment_url, headers=headers, stream=True, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Segment fetch failed: {response.status_code}")
            return jsonify({'error': 'Segment fetch failed'}), 502
        
        return Response(
            response.iter_content(chunk_size=8192),
            mimetype=response.headers.get('Content-Type', 'video/mp4'),
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Range, Content-Type',
                'Access-Control-Expose-Headers': 'Content-Length, Content-Range',
                'Cache-Control': 'public, max-age=3600'
            }
        )
        
    except Exception as e:
        logger.error(f"Segment proxy error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/fetch-stream')
def fetch_stream():
    """Fetch actual stream URL from servertvhub.site PHP endpoints"""
    try:
        url = request.args.get('url', '')
        
        if not url:
            return jsonify({'error': 'No URL provided'}), 400
        
        logger.info(f"🔍 Fetching stream from: {url}")
        
        # Prepare headers to mimic browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://servertvhub.site/',
            'Origin': 'https://servertvhub.site',
            'X-Requested-With': 'XMLHttpRequest'
        }
        
        # Fetch the PHP endpoint
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            logger.error(f"❌ Failed to fetch: HTTP {response.status_code}")
            return jsonify({'error': f'HTTP {response.status_code}'}), response.status_code
        
        content = response.text
        logger.info(f"✅ Received response ({len(content)} bytes)")
        
        # Try to parse as JSON first
        try:
            data = response.json()
            logger.info(f"📦 Parsed as JSON: {data}")
            
            # Return the JSON data - let the frontend extract the URL
            return jsonify(data), 200
            
        except json.JSONDecodeError:
            # If not JSON, might be plain text URL
            logger.info(f"📝 Response is plain text")
            
            # Check if it's a direct stream URL
            if content.strip().startswith('http') and ('.m3u8' in content or '.mpd' in content):
                return jsonify({'url': content.strip()}), 200
            
            # Try to extract URL from HTML/text
            import re
            url_pattern = r'https?://[^\s<>"]+\.(?:m3u8|mpd)[^\s<>"]*'
            urls = re.findall(url_pattern, content)
            
            if urls:
                logger.info(f"🔗 Extracted URL: {urls[0]}")
                return jsonify({'url': urls[0]}), 200
            
            logger.error(f"❌ Could not extract stream URL from response")
            return jsonify({
                'error': 'Could not extract stream URL',
                'content_preview': content[:200]
            }), 400
            
    except requests.Timeout:
        logger.error(f"⏱️ Timeout fetching stream")
        return jsonify({'error': 'Request timeout'}), 504
    except Exception as e:
        logger.error(f"❌ Error fetching stream: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/channels')
def api_channels():
    channels = get_all_channels()
    formatted = []
    
    for cid, ch in channels.items():
        formatted.append({
            'id': cid,
            'name': ch['name'],
            'logo': ch.get('logo', ''),
            'link': ch.get('link', ''),
            'category': ch.get('category', 'Other'),
            'stream_type': ch.get('stream_type', 'dash')
        })
    
    return jsonify(formatted)

@app.route('/health')
def health():
    stats = get_stats()
    return jsonify({
        'status': 'ok',
        'mongodb': MONGO_ENABLED,
        'gemini': gemini_model is not None,
        **stats
    })

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ============= AI CATEGORIZATION =============

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
    
    return categorize_basic(channel_name)

def categorize_basic(name):
    """Enhanced keyword-based categorization"""
    n = name.lower()
    
    keywords = {
        'Sports': ['sport', 'cricket', 'football', 'fifa', 'hockey', 'espn', 'star sports', 
                   'sony ten', 'euro sport', 'premier league', 'tennis', 'basketball', 'nba',
                   'women sports', 'athletics', 'olympics'],
        'News': ['news', 'ndtv', 'aaj tak', 'abp', 'zee news', 'india today', 'republic', 
                 'times now', 'cnn', 'bbc', 'fox news', 'cnbc', 'breaking'],
        'Movies': ['movie', 'cinema', 'pictures', 'pix', 'flix', 'max', 'hbo', 'film',
                   'hollywood', 'bollywood'],
        'Music': ['music', 'mtv', '9xm', 'zoom', 'vh1', 'bindass', 'songs', 'radio'],
        'Kids': ['kids', 'cartoon', 'nick', 'pogo', 'disney', 'sonic', 'hungama', 'junior',
                 'children', 'toon'],
        'Documentary': ['discovery', 'national geo', 'nat geo', 'animal planet', 'history', 
                        'tlc', 'wild', 'science', 'investigation'],
        'Entertainment': ['star', 'sony', 'zee', 'colors', '&tv', 'sab', 'bharat', 'plus',
                          'general entertainment', 'drama', 'reality'],
        'Religious': ['aastha', 'sanskar', 'god', 'ishwar', 'devotional', 'spiritual',
                      'religious', 'temple', 'church']
    }
    
    for category, words in keywords.items():
        if any(word in n for word in words):
            return category
    
    return 'Other'

# ============= DATA PARSING =============

def parse_json_channels(content, source_info="unknown"):
    """Parse JSON format channels with better duplicate handling"""
    
    # Skip empty content
    if not content or len(content.strip()) < 10:
        logger.error("❌ Content is empty or too short")
        return False
    
    # Check if processed recently (10 minutes only)
    content_hash = hashlib.md5(content.encode()).hexdigest()
    
    if MONGO_ENABLED:
        ten_mins_ago = datetime.now() - timedelta(minutes=10)
        recent = sources_col.find_one({
            'hash': content_hash,
            'processed_at': {'$gte': ten_mins_ago}
        })
        if recent:
            logger.info(f"⏭️ Same source processed {(datetime.now() - recent['processed_at']).seconds} seconds ago")
            return True
    
    try:
        data = json.loads(content) if isinstance(content, str) else content
        
        channels_list = []
        if isinstance(data, list):
            channels_list = data
        elif isinstance(data, dict) and 'channels' in data:
            channels_list = data['channels']
        else:
            logger.error("❌ Invalid JSON format - expected array or object with 'channels' key")
            return False
        
        logger.info(f"✅ Found {len(channels_list)} channels in JSON")
        
        saved_count = 0
        updated_count = 0
        error_count = 0
        
        for idx, ch in enumerate(channels_list):
            try:
                cid = ch.get('id', f"ch_{idx}_{hashlib.md5(ch.get('name', 'unknown').encode()).hexdigest()[:8]}")
                
                channel_data = {
                    'id': cid,
                    'name': ch.get('name', 'Unknown'),
                    'link': ch.get('link', ch.get('url', '')),
                    'logo': ch.get('logo', ''),
                    'drmScheme': ch.get('drmScheme', ''),
                    'drmLicense': ch.get('drmLicense', ''),
                    'cookie': ch.get('cookie', ''),
                    'category': ch.get('category'),
                    'stream_type': ch.get('stream_type', 'dash'),
                    'updated_at': datetime.now().isoformat(),
                    'needs_category': not ch.get('category')
                }
                
                # Check if exists
                existing = get_channel(cid)
                if existing:
                    logger.info(f"  ↻ Updating: {ch.get('name', 'Unknown')}")
                    updated_count += 1
                else:
                    logger.info(f"  ✓ Adding: {ch.get('name', 'Unknown')}")
                    saved_count += 1
                
                save_channel(channel_data)
                
                # Progress logging
                if (idx + 1) % 10 == 0:
                    logger.info(f"  📊 Progress: {idx + 1}/{len(channels_list)} channels processed")
                
            except Exception as e:
                logger.error(f"  ✗ Error processing channel {idx}: {e}")
                error_count += 1
                continue
        
        # Mark as processed
        if MONGO_ENABLED:
            sources_col.update_one(
                {'hash': content_hash},
                {'$set': {
                    'hash': content_hash,
                    'source': source_info,
                    'processed_at': datetime.now(),
                    'channel_count': len(channels_list),
                    'new': saved_count,
                    'updated': updated_count,
                    'errors': error_count
                }},
                upsert=True
            )
        
        logger.info(f"""
✅ JSON Import Complete:
   • Total Found: {len(channels_list)}
   • New: {saved_count}
   • Updated: {updated_count}
   • Errors: {error_count}
""")
        
        return True
    
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON parse error: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Error processing JSON: {e}")
        import traceback
        traceback.print_exc()
        return False

async def parse_m3u_playlist(content, source_url='', source_info='unknown'):
    """Parse M3U playlist with improved duplicate checking"""
    
    # Skip duplicate check if content is empty
    if not content or len(content.strip()) < 10:
        logger.error("❌ Content is empty or too short")
        return False
    
    # Create a unique hash based on content
    content_hash = hashlib.md5(content.encode()).hexdigest()
    
    # Check if processed in last 10 minutes only (more lenient)
    if MONGO_ENABLED:
        ten_mins_ago = datetime.now() - timedelta(minutes=10)
        recent = sources_col.find_one({
            'hash': content_hash,
            'processed_at': {'$gte': ten_mins_ago}
        })
        if recent:
            logger.info(f"⏭️ Same source processed {(datetime.now() - recent['processed_at']).seconds} seconds ago")
            # Still return the count as success
            return True
    
    try:
        # Determine base URL for relative paths
        base_url = ''
        if source_url:
            parsed = urlparse(source_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            logger.info(f"🔗 Base URL: {base_url}")
        
        # Handle servertvhub.site style
        if 'servertvhub.site' in source_url or 'playlist.php' in source_url:
            logger.info("🔍 Detected servertvhub.site playlist")
            channels_list = parse_servertvhub_playlist(content, base_url)
        else:
            logger.info("🔍 Parsing standard M3U playlist")
            channels_list = parse_m3u_content(content, base_url)
        
        if not channels_list:
            logger.error("❌ No channels found in playlist")
            return False
        
        logger.info(f"✅ Found {len(channels_list)} channels in playlist")
        
        # Save channels with progress logging
        saved_count = 0
        skipped_count = 0
        error_count = 0
        
        for idx, ch in enumerate(channels_list):
            try:
                # Generate unique ID based on name and link
                unique_str = f"{ch['name']}_{ch['link']}"
                unique_hash = hashlib.md5(unique_str.encode()).hexdigest()[:8]
                cid = ch.get('id', f"m3u_{unique_hash}")
                
                channel_data = {
                    'id': cid,
                    'name': ch['name'],
                    'link': ch['link'],
                    'logo': ch.get('logo', ''),
                    'category': ch.get('category'),
                    'stream_type': ch.get('stream_type', 'hls'),
                    'needs_proxy': ch.get('needs_proxy', False),
                    'is_php_endpoint': ch.get('is_php_endpoint', False),
                    'updated_at': datetime.now().isoformat(),
                    'needs_category': not ch.get('category')
                }
                
                # Check if channel already exists
                existing = get_channel(cid)
                if existing:
                    logger.info(f"  ↻ Updating: {ch['name']}")
                    skipped_count += 1
                else:
                    logger.info(f"  ✓ Adding: {ch['name']}")
                
                save_channel(channel_data)
                saved_count += 1
                
                # Log progress every 10 channels
                if (idx + 1) % 10 == 0:
                    logger.info(f"  📊 Progress: {idx + 1}/{len(channels_list)} channels processed")
                
            except Exception as e:
                logger.error(f"  ✗ Error saving channel {idx} ({ch.get('name', 'Unknown')}): {e}")
                error_count += 1
                continue
        
        # Mark source as processed
        if MONGO_ENABLED:
            sources_col.update_one(
                {'hash': content_hash},
                {'$set': {
                    'hash': content_hash,
                    'source': source_info,
                    'source_url': source_url,
                    'processed_at': datetime.now(),
                    'channel_count': saved_count,
                    'total_found': len(channels_list),
                    'skipped': skipped_count,
                    'errors': error_count
                }},
                upsert=True
            )
        
        logger.info(f"""
✅ M3U Import Complete:
   • Total Found: {len(channels_list)}
   • New/Updated: {saved_count}
   • Already Existed: {skipped_count}
   • Errors: {error_count}
""")
        
        return True
    
    except Exception as e:
        logger.error(f"❌ M3U parse error: {e}")
        import traceback
        traceback.print_exc()
        return False

async def auto_categorize_all():
    """Auto-categorize channels without category"""
    channels = get_all_channels()
    uncategorized = [(cid, ch) for cid, ch in channels.items() 
                     if ch.get('needs_category', False)]
    
    if not uncategorized:
        logger.info("✅ All channels already categorized")
        return
    
    logger.info(f"🤖 Categorizing {len(uncategorized)} channels...")
    
    for idx, (cid, ch) in enumerate(uncategorized, 1):
        try:
            category = await categorize_with_ai(ch['name'])
            ch['category'] = category
            ch['needs_category'] = False
            save_channel(ch)
            
            logger.info(f"[{idx}/{len(uncategorized)}] {ch['name']} → {category}")
            
            if gemini_model:
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Categorization error: {e}")
            ch['category'] = 'Other'
            save_channel(ch)
    
    logger.info("✅ Categorization complete!")

async def load_from_url(url):
    """Load playlist from URL with better error handling"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/'
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status == 200:
                    content = await response.text()
                    logger.info(f"✅ Successfully loaded URL: {url} ({len(content)} bytes)")
                    return content
                else:
                    logger.error(f"❌ Failed to load URL: HTTP {response.status}")
                    return None
    except asyncio.TimeoutError:
        logger.error(f"⏱️ Timeout loading URL: {url}")
        return None
    except Exception as e:
        logger.error(f"❌ URL load error: {e}")
        return None

# ============= PAGINATION HELPERS =============

def paginate_list(items, page, per_page):
    """Paginate a list of items"""
    start = page * per_page
    end = start + per_page
    return items[start:end], len(items)

def create_pagination_keyboard(items, page, per_page, callback_prefix, back_callback="start", columns=2):
    """Create paginated keyboard with 2 columns"""
    current_items, total_items = paginate_list(items, page, per_page)
    total_pages = (total_items + per_page - 1) // per_page
    
    keyboard = []
    
    # Create rows with specified columns
    for i in range(0, len(current_items), columns):
        row = []
        for item in current_items[i:i+columns]:
            row.append(item)
        keyboard.append(row)
    
    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Back", callback_data=f"{callback_prefix}_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="noop"))
    
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{callback_prefix}_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data=back_callback)])
    
    return keyboard

# ============= TELEGRAM BOT HANDLERS =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main menu with paginated categories (2 columns)"""
    user = update.effective_user
    update_stats('users', user.id)
    
    if bot_settings['maintenance_mode'] and not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("🔧 Bot under maintenance!")
        else:
            await update.message.reply_text("🔧 Bot under maintenance!")
        return
    
    categories = get_categories()
    categories_list = sorted(categories.keys())
    
    cat_buttons = []
    for cat in categories_list:
        cat_buttons.append(InlineKeyboardButton(
            f"📺 {cat} ({len(categories[cat])})", 
            callback_data=f"cat_{cat}_0"
        ))
    
    keyboard = create_pagination_keyboard(
        cat_buttons,
        0,
        CATEGORIES_PER_PAGE,
        "categories",
        "start",
        columns=2
    )
    
    keyboard.insert(-1, [InlineKeyboardButton("🔍 Search Channels", switch_inline_query_current_chat="")])
    
    if is_admin(user.id):
        keyboard.insert(-1, [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    
    stats = get_stats()
    text = f"""
🎬 <b>{bot_settings['bot_name']}</b>

👋 Hi {user.first_name}!

📺 Total Channels: {stats['channels']}
🗂 Categories: {stats['categories']}
▶️ Total Plays: {stats['plays']}

<b>Select a category to browse channels:</b>
"""
    
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            text, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

async def categories_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category pagination"""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split('_')[-1])
    
    categories = get_categories()
    categories_list = sorted(categories.keys())
    
    cat_buttons = []
    for cat in categories_list:
        cat_buttons.append(InlineKeyboardButton(
            f"📺 {cat} ({len(categories[cat])})", 
            callback_data=f"cat_{cat}_0"
        ))
    
    keyboard = create_pagination_keyboard(
        cat_buttons,
        page,
        CATEGORIES_PER_PAGE,
        "categories",
        "start",
        columns=2
    )
    
    keyboard.insert(-1, [InlineKeyboardButton("🔍 Search Channels", switch_inline_query_current_chat="")])
    
    if is_admin(query.from_user.id):
        keyboard.insert(-1, [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    
    stats = get_stats()
    text = f"""
🎬 <b>{bot_settings['bot_name']}</b>

📺 Total Channels: {stats['channels']}
🗂 Categories: {stats['categories']}

<b>Select a category to browse channels:</b>
"""
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated channels in a category (2 columns, full names)"""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split('_')
    page = int(parts[-1])
    cat = '_'.join(parts[1:-1])
    
    channels = get_channels_by_category(cat)
    
    if not channels:
        await query.answer("No channels in this category!", show_alert=True)
        return
    
    channel_buttons = []
    for ch in channels:
        # Use full channel name (truncate only if very long)
        name = ch['name'][:40] + '...' if len(ch['name']) > 40 else ch['name']
        channel_buttons.append(InlineKeyboardButton(
            f"▶️ {name}", 
            callback_data=f"play_{ch['id']}"
        ))
    
    keyboard = create_pagination_keyboard(
        channel_buttons,
        page,
        CHANNELS_PER_PAGE,
        f"cat_{cat}",
        "start",
        columns=2
    )
    
    text = f"""
📺 <b>{cat}</b>

Total: {len(channels)} channels

<i>Select a channel to watch:</i>
"""
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def play_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open channel in mini player"""
    query = update.callback_query
    await query.answer("🎬 Opening player...")
    
    cid = query.data.replace('play_', '')
    ch = get_channel(cid)
    
    if not ch:
        await query.answer("❌ Channel not found!", show_alert=True)
        return
    
    update_stats('plays', 1)
    
    player_url = f"{WEBAPP_URL}/player?id={cid}"
    
    keyboard = [
        [InlineKeyboardButton("🎬 Watch Now", web_app=WebAppInfo(url=player_url))],
        [InlineKeyboardButton("🔙 Back", callback_data=f"cat_{ch.get('category', 'Other')}_0")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="start")]
    ]
    
    info_text = f"""
🎬 <b>{ch['name']}</b>

📂 Category: {ch.get('category', 'Other')}
🔐 DRM: {ch.get('drmScheme', 'None')}
📡 Type: {ch.get('stream_type', 'DASH').upper()}

<i>Click "Watch Now" to open the player</i>
"""
    
    await query.message.edit_text(
        info_text,
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
        [
            InlineKeyboardButton("📤 Upload JSON", callback_data="admin_upload_json"),
            InlineKeyboardButton("📤 Upload M3U", callback_data="admin_upload_m3u")
        ],
        [
            InlineKeyboardButton("🔗 Load JSON URL", callback_data="admin_url_json"),
            InlineKeyboardButton("🔗 Load M3U URL", callback_data="admin_url_m3u")
        ],
        [InlineKeyboardButton("🤖 AI Categorize", callback_data="admin_categorize")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [
            InlineKeyboardButton("🗑️ Clear Cache", callback_data="admin_clear_cache"),
            InlineKeyboardButton("🗑️ Clear All", callback_data="admin_clear")
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="start")]
    ]
    
    stats = get_stats()
    text = f"""
⚙️ <b>Admin Panel</b>

📺 Channels: {stats['channels']}
🗂 Categories: {stats['categories']}
👥 Unique Users: {stats['users']}
▶️ Total Plays: {stats['plays']}

🤖 Gemini AI: {'✅ Active' if gemini_model else '❌ Inactive'}
💾 MongoDB: {'✅ Connected' if MONGO_ENABLED else '❌ Using Memory'}

<i>Select an option below:</i>
"""
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only!")
        return
    
    file = update.message.document
    if not file:
        await update.message.reply_text("❌ Please send a file!")
        return
    
    file_type = context.user_data.get('expecting_file_type', 'json')
    
    if file_type == 'json' and not file.file_name.endswith('.json'):
        await update.message.reply_text("❌ Please send a .json file!")
        return
    elif file_type == 'm3u' and not (file.file_name.endswith('.m3u') or file.file_name.endswith('.m3u8')):
        await update.message.reply_text("❌ Please send a .m3u or .m3u8 file!")
        return
    
    msg = await update.message.reply_text(
        f"⏳ <b>Processing {file.file_name}</b>\n\n📥 Downloading file...",
        parse_mode='HTML'
    )
    
    try:
        file_obj = await context.bot.get_file(file.file_id)
        content = await file_obj.download_as_bytearray()
        content_str = content.decode('utf-8')
        
        await msg.edit_text(
            f"✅ <b>File Downloaded!</b>\n\n📦 Size: {len(content_str)} bytes\n🔄 Parsing {file_type.upper()} data...",
            parse_mode='HTML'
        )
        
        success = False
        
        if file_type == 'json':
            success = parse_json_channels(content_str, f"file:{file.file_name}")
        elif file_type == 'm3u':
            success = await parse_m3u_playlist(content_str, '', f"file:{file.file_name}")
        
        if success:
            await msg.edit_text(
                f"✅ <b>Parsing Complete!</b>\n\n🤖 Starting AI categorization...",
                parse_mode='HTML'
            )
            await auto_categorize_all()
            stats = get_stats()
            
            await msg.edit_text(
                f"🎉 <b>Successfully Loaded!</b>\n\n📺 Total Channels: {stats['channels']}\n🗂 Categories: {stats['categories']}\n\n<i>Use /start to browse channels</i>",
                parse_mode='HTML'
            )
        else:
            await msg.edit_text(
                f"❌ <b>Parsing Failed!</b>\n\n⚠️ Possible reasons:\n• Invalid {file_type.upper()} format\n• Source already processed\n• Empty or corrupted data\n\n<i>Check logs for details</i>",
                parse_mode='HTML'
            )
    
    except Exception as e:
        logger.error(f"File error: {e}")
        import traceback
        traceback.print_exc()
        await msg.edit_text(
            f"❌ <b>Error Processing File!</b>\n\n⚠️ Error: <code>{str(e)}</code>",
            parse_mode='HTML'
        )
    
    finally:
        context.user_data.pop('expecting_file_type', None)

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "noop":
        await query.answer()
        return
    
    if data == "start":
        await query.answer()
        await start(update, context)
    elif data.startswith("categories_page_"):
        await categories_page_handler(update, context)
    elif data.startswith("cat_") and "_page_" in data:
        # Handle category pagination - extract page number and category
        parts = data.split('_')
        # Find 'page' index
        page_index = parts.index('page')
        page = int(parts[page_index + 1])
        # Everything between 'cat' and 'page' is the category name
        cat = '_'.join(parts[1:page_index])
        
        # Call category handler with reconstructed data
        await category_handler_with_page(update, context, cat, page)
    elif data.startswith("cat_"):
        await category_handler(update, context)
    elif data.startswith("play_"):
        await play_handler(update, context)
    elif data == "admin":
        await admin_handler(update, context)
    elif data == "admin_categorize":
        await query.answer("🤖 Starting AI categorization...")
        msg = await query.message.edit_text("🤖 <b>AI Categorization in Progress...</b>\n\n⏳ Please wait...", parse_mode='HTML')
        await auto_categorize_all()
        await msg.edit_text("✅ <b>Categorization Complete!</b>\n\n<i>Returning to admin panel...</i>", parse_mode='HTML')
        await asyncio.sleep(1)
        await admin_handler(update, context)
    elif data == "admin_upload_json":
        await query.answer()
        context.user_data['expecting_file_type'] = 'json'
        await query.message.edit_text(
            "📤 <b>Upload JSON File</b>\n\n<b>Required format:</b>\n<code>[\n  {\n    \"name\": \"Channel Name\",\n    \"link\": \"stream_url\",\n    \"logo\": \"logo_url\",\n    \"drmScheme\": \"clearkey\",\n    \"drmLicense\": \"key:id\",\n    \"cookie\": \"cookie_string\"\n  }\n]</code>\n\n<i>Send your .json file now</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_upload_m3u":
        await query.answer()
        context.user_data['expecting_file_type'] = 'm3u'
        await query.message.edit_text(
            "📤 <b>Upload M3U/M3U8 File</b>\n\n<b>Supported format:</b>\n<code>#EXTINF:-1 tvg-id=\"id\" tvg-name=\"name\" tvg-logo=\"logo\" group-title=\"category\",Channel Name\nhttp://stream-url.m3u8</code>\n\n<i>Send your .m3u or .m3u8 file now</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_url_json":
        await query.answer()
        context.user_data['awaiting_url'] = 'json'
        await query.message.edit_text(
            "🔗 <b>Load JSON from URL</b>\n\n<i>Send the JSON URL now:</i>\n\nExample:\n<code>https://example.com/channels.json</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_url_m3u":
        await query.answer()
        context.user_data['awaiting_url'] = 'm3u'
        await query.message.edit_text(
            "🔗 <b>Load M3U from URL</b>\n\n<i>Send the M3U/M3U8 URL now:</i>\n\nExamples:\n<code>https://example.com/playlist.m3u8\nhttps://servertvhub.site/playlist.php</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_stats":
        await query.answer()
        categories = get_categories()
        cat_list = "\n".join([f"• <b>{c}</b>: {len(ch)} channels" for c, ch in sorted(categories.items())[:15]])
        stats = get_stats()
        
        await query.message.edit_text(
            f"📊 <b>Detailed Statistics</b>\n\n<b>Categories:</b>\n{cat_list}\n\n💾 Storage: {'MongoDB' if MONGO_ENABLED else 'Memory Cache'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_clear":
        await query.answer("⚠️ This will delete all data!", show_alert=True)
        keyboard = [
            [InlineKeyboardButton("❌ Confirm Delete All", callback_data="admin_clear_confirm")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="admin")]
        ]
        await query.message.edit_text(
            "⚠️ <b>Warning!</b>\n\n<i>This will permanently delete all channels and categories. Are you sure?</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    elif data == "admin_clear_confirm":
        await query.answer()
        if MONGO_ENABLED:
            channels_col.delete_many({})
            sources_col.delete_many({})
        else:
            channels_cache.clear()
            categories_cache.clear()
        
        await query.message.edit_text(
            "✅ <b>Database Cleared!</b>\n\n<i>All channels and categories have been deleted.</i>",
            parse_mode='HTML'
        )
        await asyncio.sleep(2)
        await admin_handler(update, context)
    elif data == "admin_clear_cache":
        await query.answer("🗑️ Clearing duplicate check cache...")
        if MONGO_ENABLED:
            # Only clear old source records (older than 1 hour)
            one_hour_ago = datetime.now() - timedelta(hours=1)
            result = sources_col.delete_many({'processed_at': {'$lt': one_hour_ago}})
            await query.message.edit_text(
                f"✅ <b>Cache Cleared!</b>\n\n🗑️ Removed {result.deleted_count} old source records\n\n<i>You can now re-import sources</i>",
                parse_mode='HTML'
            )
        else:
            await query.message.edit_text(
                "ℹ️ <b>Cache Not Applicable</b>\n\n<i>Memory mode doesn't use source tracking</i>",
                parse_mode='HTML'
            )
        await asyncio.sleep(2)
        await admin_handler(update, context)

async def category_handler_with_page(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: str, page: int):
    """Show paginated channels in a category with specific page"""
    query = update.callback_query
    await query.answer()
    
    channels = get_channels_by_category(cat)
    
    if not channels:
        await query.answer("No channels in this category!", show_alert=True)
        return
    
    channel_buttons = []
    for ch in channels:
        name = ch['name'][:40] + '...' if len(ch['name']) > 40 else ch['name']
        channel_buttons.append(InlineKeyboardButton(
            f"▶️ {name}", 
            callback_data=f"play_{ch['id']}"
        ))
    
    keyboard = create_pagination_keyboard(
        channel_buttons,
        page,
        CHANNELS_PER_PAGE,
        f"cat_{cat}",
        "start",
        columns=2
    )
    
    text = f"""
📺 <b>{cat}</b>

Total: {len(channels)} channels
Page: {page + 1}

<i>Select a channel to watch:</i>
"""
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
        
        # Reconstruct the callback data
    if data.startswith("catpage_"):
        cat, page = data.replace("catpage_", "").rsplit("_", 1)
        page = int(page)
        context.user_data['current_category'] = cat
        query.data = f"cat_{cat}_{page}"
        await category_handler(update, context)
    elif data.startswith("cat_"):
        await category_handler(update, context)
    elif data.startswith("play_"):
        await play_handler(update, context)
    elif data == "admin":
        await admin_handler(update, context)
    elif data == "admin_categorize":
        await query.answer("🤖 Starting AI categorization...", show_alert=True)
        await auto_categorize_all()
        await query.answer("✅ Categorization complete!", show_alert=True)
        await admin_handler(update, context)
    elif data == "admin_upload_json":
        await query.answer()
        context.user_data['expecting_file_type'] = 'json'
        await query.message.edit_text(
            "📤 <b>Upload JSON File</b>\n\n<b>Required format:</b>\n<code>[\n  {\n    \"name\": \"Channel Name\",\n    \"link\": \"stream_url\",\n    \"logo\": \"logo_url\",\n    \"drmScheme\": \"clearkey\",\n    \"drmLicense\": \"key:id\",\n    \"cookie\": \"cookie_string\"\n  }\n]</code>\n\n<i>Send your .json file now</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_upload_m3u":
        await query.answer()
        context.user_data['expecting_file_type'] = 'm3u'
        await query.message.edit_text(
            "📤 <b>Upload M3U/M3U8 File</b>\n\n<b>Supported format:</b>\n<code>#EXTINF:-1 tvg-id=\"id\" tvg-name=\"name\" tvg-logo=\"logo\" group-title=\"category\",Channel Name\nhttp://stream-url.m3u8</code>\n\n<i>Send your .m3u or .m3u8 file now</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_url_json":
        context.user_data['awaiting_url'] = 'json'
        await query.message.edit_text(
            "🔗 <b>Load JSON from URL</b>\n\n<i>Send the JSON URL now:</i>\n\nExample:\n<code>https://example.com/channels.json</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_url_m3u":
        context.user_data['awaiting_url'] = 'm3u'
        await query.message.edit_text(
            "🔗 <b>Load M3U from URL</b>\n\n<i>Send the M3U/M3U8 URL now:</i>\n\nExamples:\n<code>https://example.com/playlist.m3u8\nhttps://servertvhub.site/playlist.php</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_stats":
        categories = get_categories()
        cat_list = "\n".join([f"• <b>{c}</b>: {len(ch)} channels" for c, ch in sorted(categories.items())[:15]])
        stats = get_stats()
        
        await query.message.edit_text(
            f"📊 <b>Detailed Statistics</b>\n\n<b>Categories:</b>\n{cat_list}\n\n💾 Storage: {'MongoDB' if MONGO_ENABLED else 'Memory Cache'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin")]]),
            parse_mode='HTML'
        )
    elif data == "admin_clear":
        await query.answer("⚠️ This will delete all data!", show_alert=True)
        keyboard = [
            [InlineKeyboardButton("❌ Confirm Delete All", callback_data="admin_clear_confirm")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="admin")]
        ]
        await query.message.edit_text(
            "⚠️ <b>Warning!</b>\n\n<i>This will permanently delete all channels and categories. Are you sure?</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    elif data == "admin_clear_confirm":
        if MONGO_ENABLED:
            channels_col.delete_many({})
            sources_col.delete_many({})
            await query.answer("✅ Database cleared successfully!", show_alert=True)
        else:
            channels_cache.clear()
            categories_cache.clear()
            await query.answer("✅ Cache cleared successfully!", show_alert=True)
        await admin_handler(update, context)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (URL loading)"""
    if context.user_data.get('awaiting_url') and is_admin(update.effective_user.id):
        url_type = context.user_data.get('awaiting_url')
        context.user_data['awaiting_url'] = None
        
        url = update.message.text.strip()
        
        msg = await update.message.reply_text(
            f"⏳ <b>Loading from URL...</b>\n\n🔗 URL: <code>{url}</code>\n📥 Downloading content...",
            parse_mode='HTML'
        )
        
        try:
            content = await load_from_url(url)
            
            if not content:
                await msg.edit_text(
                    f"❌ <b>Failed to Load URL!</b>\n\n🔗 URL: <code>{url}</code>\n\n<i>Please check:\n• URL is accessible\n• Network connection\n• URL format is correct</i>",
                    parse_mode='HTML'
                )
                return
            
            await msg.edit_text(
                f"✅ <b>Content Downloaded!</b>\n\n📦 Size: {len(content)} bytes\n🔄 Parsing {url_type.upper()} data...",
                parse_mode='HTML'
            )
            
            success = False
            
            if url_type == 'json':
                success = parse_json_channels(content, f"url:{url}")
            elif url_type == 'm3u':
                success = await parse_m3u_playlist(content, url, f"url:{url}")
            
            if success:
                await msg.edit_text(
                    f"✅ <b>Parsing Complete!</b>\n\n🤖 Starting AI categorization...",
                    parse_mode='HTML'
                )
                await auto_categorize_all()
                
                stats = get_stats()
                await msg.edit_text(
                    f"🎉 <b>Successfully Loaded!</b>\n\n📺 Total Channels: {stats['channels']}\n🗂 Categories: {stats['categories']}\n\n<i>Use /start to browse channels</i>",
                    parse_mode='HTML'
                )
            else:
                await msg.edit_text(
                    f"❌ <b>Parsing Failed!</b>\n\n⚠️ Possible reasons:\n• Invalid {url_type.upper()} format\n• Source already processed\n• Empty or corrupted data\n\n<i>Please check the URL and try again</i>",
                    parse_mode='HTML'
                )
                
        except Exception as e:
            logger.error(f"URL loading error: {e}")
            await msg.edit_text(
                f"❌ <b>Error Loading URL!</b>\n\n⚠️ Error: <code>{str(e)}</code>\n\n<i>Please try again or contact admin</i>",
                parse_mode='HTML'
            )

def main():
    # Start Flask
    Thread(target=run_flask, daemon=True).start()
    
    # Start Bot
    app_bot = Application.builder().token(BOT_TOKEN).build()
    
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(callback_router))
    app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    logger.info("🚀 Bot started successfully!")
    logger.info(f"📡 Web Server: {WEBAPP_URL}")
    logger.info(f"🤖 Gemini AI: {'ENABLED' if gemini_model else 'DISABLED'}")
    logger.info(f"💾 MongoDB: {'CONNECTED' if MONGO_ENABLED else 'DISABLED'}")
    logger.info(f"📄 Categories per page: {CATEGORIES_PER_PAGE} (2 columns)")
    logger.info(f"📺 Channels per page: {CHANNELS_PER_PAGE} (2 columns)")
    
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
