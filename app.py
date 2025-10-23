from flask import Flask, render_template, request, jsonify
import os
import threading
import logging
from ipytv import playlist
from ipytv.playlist import M3UPlaylist, IPTVAttr
import requests
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
M3U_URL = os.environ.get('M3U_URL', '')

class ChannelManager:
    def __init__(self):
        self.channels = {}
        self.categories = {}
        self.last_update = 0
        self.cache_duration = 3600  # 1 hour
        self.loading = False
        self.playlist_obj = None
        
    def get_channels(self):
        """Get cached channels"""
        return self.channels.copy()
    
    def parse_m3u_direct(self, url):
        """
        BEST METHOD: Manual fetch with proper headers + IPyTV parsing
        - Custom headers for dynamic PHP endpoints
        - Works with stream apps and browsers
        - Proper User-Agent spoofing
        """
        if self.loading:
            logger.info("⏳ Already loading - skipping")
            return False
        
        self.loading = True
        start_time = time.time()
        
        try:
            logger.info("="*70)
            logger.info("🌟 ENHANCED M3U LOADING WITH CUSTOM HEADERS 🌟")
            logger.info("="*70)
            logger.info(f"🔗 URL: {url}")
            logger.info("🔧 Method: Manual fetch + IPyTV parsing")
            logger.info("="*70)
            
            # Import requests here
            import requests
            
            # Custom headers that mimic IPTV player apps
            headers = {
                'User-Agent': 'VLC/3.0.18 LibVLC/3.0.18',  # VLC user agent
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Referer': 'https://public.kliv.fun/',
                'Origin': 'https://public.kliv.fun'
            }
            
            logger.info("📡 Fetching M3U with IPTV player headers...")
            logger.info(f"🔧 User-Agent: {headers['User-Agent']}")
            
            # Fetch with custom headers
            session = requests.Session()
            response = session.get(
                url,
                headers=headers,
                timeout=120,
                allow_redirects=True,
                stream=False
            )
            
            response.raise_for_status()
            content = response.text
            
            logger.info(f"✅ Fetched {len(content):,} bytes")
            
            if not content.strip():
                logger.error("❌ Empty content received")
                return False
            
            # Save to temp file
            temp_file = '/tmp/playlist_custom.m3u'
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            logger.info(f"💾 Saved to {temp_file}")
            
            # Now parse with IPyTV
            logger.info("🔄 Parsing with IPyTV...")
            m3u_playlist = playlist.loadf(temp_file)
            
            if not m3u_playlist or len(m3u_playlist) == 0:
                logger.error("❌ IPyTV returned empty playlist or failed to load")
                return False
            
            self.playlist_obj = m3u_playlist
            load_time = time.time() - start_time
            logger.info(f"✅ IPyTV loaded {len(m3u_playlist):,} entries in {load_time:.2f}s")
            
            # Convert to our format
            logger.info("🔄 Converting to internal format...")
            channels = {}
            categories = {}
            skipped = 0
            valid = 0
            
            for idx, channel in enumerate(m3u_playlist):
                # Progress logging for large playlists
                if (idx + 1) % 500 == 0:
                    logger.info(f"📊 Processing: {idx + 1:,}/{len(m3u_playlist):,}")
                
                # Extract channel info using IPyTV attributes
                name = channel.name or f'Channel {idx + 1}'
                logo = channel.attributes.get(IPTVAttr.TVG_LOGO.value, '')
                category = channel.attributes.get(IPTVAttr.GROUP_TITLE.value, 'Uncategorized')
                tvg_id = channel.attributes.get(IPTVAttr.TVG_ID.value, '')
                url = channel.url or ''
                
                # Skip channels without URL
                if not url or url.strip() == '':
                    skipped += 1
                    continue
                
                valid += 1
                
                channel_data = {
                    'name': name.strip(),
                    'logo': logo.strip() if logo else '',
                    'category': category.strip(),
                    'tvg_id': tvg_id.strip() if tvg_id else '',
                    'url': url.strip()
                }
                
                # Add to categories
                if category not in categories:
                    categories[category] = []
                categories[category].append(channel_data.copy())
                
                # Add to channels dict with sequential ID
                channel_id = len(channels)
                channels[channel_id] = channel_data
            
            if channels:
                self.channels = channels
                self.categories = categories
                self.last_update = time.time()
                
                elapsed = time.time() - start_time
                
                logger.info("="*70)
                logger.info("🎉 SUCCESS! CHANNELS LOADED!")
                logger.info("="*70)
                logger.info(f"✅ Valid channels: {len(channels):,}")
                logger.info(f"📂 Categories: {len(categories):,}")
                logger.info(f"⏱️ Total time: {elapsed:.2f}s")
                logger.info(f"📊 Processed: {len(m3u_playlist):,} entries")
                logger.info(f"✅ Valid: {valid:,}")
                if skipped > 0:
                    logger.info(f"⚠️ Skipped: {skipped:,} (no URL)")
                logger.info(f"📈 Success rate: {valid/(valid+skipped)*100:.1f}%")
                logger.info("="*70)
                
                # Log top 15 categories
                top_cats = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)[:15]
                logger.info("📊 TOP 15 CATEGORIES:")
                for i, (cat, ch_list) in enumerate(top_cats, 1):
                    logger.info(f"  {i:2d}. 📂 {cat[:40]:40s} : {len(ch_list):,} channels")
                logger.info("="*70)
                
                return True
            else:
                logger.error("❌ No valid channels after parsing")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
        finally:
            self.loading = False

# Initialize channel manager
channel_manager = ChannelManager()

# Load channels in background (non-blocking startup)
def load_channels_async():
    """Background M3U loader"""
    if M3U_URL:
        logger.info("="*70)
        logger.info("🚀 BACKGROUND CHANNEL LOADING STARTED")
        logger.info("="*70)
        logger.info(f"🔗 URL: {M3U_URL}")
        time.sleep(3)  # Give Flask time to start properly
        
        try:
            if channel_manager.parse_m3u_direct(M3U_URL):
                logger.info("="*70)
                logger.info("✅✅✅ BACKGROUND LOAD COMPLETED SUCCESSFULLY! ✅✅✅")
                logger.info("="*70)
            else:
                logger.error("="*70)
                logger.error("❌❌❌ BACKGROUND LOAD FAILED ❌❌❌")
                logger.error("="*70)
                logger.warning("💡 Possible issues:")
                logger.warning("   - URL is invalid or unreachable")
                logger.warning("   - Server is blocking requests")
                logger.warning("   - Invalid M3U format")
                logger.warning("   - Network connectivity issues")
        except Exception as e:
            logger.error(f"❌ Background load exception: {e}")
            import traceback
            logger.error(traceback.format_exc())
    else:
        logger.warning("="*70)
        logger.warning("⚠️ NO M3U_URL CONFIGURED")
        logger.warning("="*70)
        logger.warning("Set M3U_URL environment variable to load channels")

# Start background loading
threading.Thread(target=load_channels_async, daemon=True).start()

@app.route('/')
def index():
    """Home page"""
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error rendering index: {e}")
        return f"""
        <html>
        <head><title>IPTV Service</title></head>
        <body style="font-family: Arial; padding: 20px; background: #1a1a1a; color: #fff;">
            <h1>🎬 IPTV Service</h1>
            <p>📺 Channels: <strong>{len(channel_manager.channels):,}</strong></p>
            <p>📂 Categories: <strong>{len(channel_manager.categories):,}</strong></p>
            <p>⏱️ Status: <strong>{'Loading...' if channel_manager.loading else 'Ready'}</strong></p>
            <p><a href="/health" style="color: #4CAF50;">Health Check</a></p>
        </body>
        </html>
        """, 200

@app.route('/player')
def player():
    """Player page for Mini App"""
    channel_id = request.args.get('ch', type=int)
    
    if channel_id is None:
        return "❌ Channel ID required (use ?ch=NUMBER)", 400
    
    channels = channel_manager.get_channels()
    
    if channel_id not in channels:
        logger.warning(f"Channel {channel_id} not found (total: {len(channels)})")
        return f"❌ Channel {channel_id} not found. Total channels: {len(channels)}", 404
    
    channel = channels[channel_id]
    
    try:
        return render_template('player.html', 
                             channel_name=channel['name'],
                             channel_logo=channel['logo'],
                             channel_category=channel['category'],
                             channel_id=channel_id)
    except Exception as e:
        logger.error(f"Error rendering player: {e}")
        return f"Error loading player: {e}", 500

@app.route('/api/channel/<int:channel_id>')
def get_channel(channel_id):
    """API to get channel stream URL"""
    channels = channel_manager.get_channels()
    
    if channel_id not in channels:
        return jsonify({'error': 'Channel not found', 'total_channels': len(channels)}), 404
    
    channel = channels[channel_id]
    
    return jsonify({
        'success': True,
        'channel_id': channel_id,
        'name': channel['name'],
        'url': channel['url'],
        'logo': channel['logo'],
        'category': channel['category'],
        'tvg_id': channel['tvg_id']
    })

@app.route('/api/channels')
def get_all_channels():
    """API to get all channels"""
    channels = channel_manager.get_channels()
    
    # Optional: Filter by category
    category_filter = request.args.get('category')
    if category_filter and category_filter in channel_manager.categories:
        filtered = channel_manager.categories[category_filter]
        return jsonify({
            'success': True,
            'channels': filtered,
            'total': len(filtered),
            'category': category_filter
        })
    
    return jsonify({
        'success': True,
        'channels': channels,
        'categories': list(channel_manager.categories.keys()),
        'total_channels': len(channels),
        'total_categories': len(channel_manager.categories),
        'last_update': channel_manager.last_update
    })

@app.route('/api/categories')
def get_categories():
    """API to get all categories with channel counts"""
    categories_list = [
        {
            'name': cat,
            'count': len(channels)
        }
        for cat, channels in channel_manager.categories.items()
    ]
    
    # Sort by count descending
    categories_list.sort(key=lambda x: x['count'], reverse=True)
    
    return jsonify({
        'success': True,
        'categories': categories_list,
        'total': len(categories_list)
    })

@app.route('/api/reload', methods=['POST'])
def reload_playlist():
    """API to reload M3U playlist"""
    try:
        data = request.get_json() or {}
        url = data.get('url', M3U_URL)
        
        if not url:
            return jsonify({'success': False, 'message': 'No URL provided'}), 400
        
        logger.info(f"🔄 Manual reload requested for: {url[:60]}...")
        
        if channel_manager.parse_m3u_direct(url):
            return jsonify({
                'success': True, 
                'message': 'Playlist reloaded successfully',
                'channels': len(channel_manager.channels),
                'categories': len(channel_manager.categories),
                'timestamp': time.time()
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to reload playlist'
            }), 500
    except Exception as e:
        logger.error(f"Reload error: {e}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@app.route('/health')
def health():
    """Health check - always returns 200 for Render"""
    return jsonify({
        'status': 'healthy',
        'service': 'IPTV Bot Web Service',
        'channels_loaded': len(channel_manager.channels),
        'categories': len(channel_manager.categories),
        'last_update': channel_manager.last_update,
        'last_update_ago': f"{int(time.time() - channel_manager.last_update)}s" if channel_manager.last_update > 0 else "never",
        'm3u_configured': bool(M3U_URL),
        'm3u_url': M3U_URL[:50] + '...' if M3U_URL and len(M3U_URL) > 50 else M3U_URL or 'Not set',
        'loading': channel_manager.loading,
        'timestamp': int(time.time())
    }), 200

@app.route('/api/search')
def search_channels():
    """Search channels by name"""
    query = request.args.get('q', '').lower().strip()
    
    if not query:
        return jsonify({'success': False, 'message': 'No search query provided'}), 400
    
    channels = channel_manager.get_channels()
    results = [
        {'id': ch_id, **ch_data}
        for ch_id, ch_data in channels.items()
        if query in ch_data['name'].lower()
    ]
    
    return jsonify({
        'success': True,
        'query': query,
        'results': results[:50],  # Limit to 50 results
        'total': len(results)
    })

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        'error': 'Endpoint not found',
        'available_endpoints': [
            '/',
            '/player?ch=<id>',
            '/api/channels',
            '/api/categories',
            '/api/channel/<id>',
            '/api/search?q=<query>',
            '/api/reload',
            '/health'
        ]
    }), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Server error: {e}")
    return jsonify({
        'error': 'Internal server error',
        'message': str(e)
    }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info("="*70)
    logger.info("🎬 JIO TV BOT - WEB SERVICE")
    logger.info("="*70)
    logger.info(f"🌐 Port: {port}")
    logger.info(f"📡 M3U URL: {M3U_URL if M3U_URL else '❌ Not configured'}")
    logger.info(f"🔧 Debug: {debug}")
    logger.info(f"📁 Templates: {'✅ Found' if os.path.exists('templates') else '❌ Missing'}")
    logger.info(f"🔧 Method: IPyTV Direct URL Loading (playlist.loadu)")
    logger.info("="*70)
    logger.info("🚀 Starting Flask server...")
    logger.info("💡 Channels will load in background")
    logger.info("="*70)
    
    app.run(host='0.0.0.0', port=port, debug=debug)
