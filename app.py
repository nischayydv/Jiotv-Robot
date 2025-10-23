from flask import Flask, render_template, request, jsonify
import os
import threading
import logging
from ipytv import playlist
from ipytv.playlist import M3UPlaylist, IPTVAttr
import httpx
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
    
    def fetch_m3u_with_httpx(self, url, timeout=20):
        """Fetch M3U content using httpx (better than requests)"""
        try:
            logger.info(f"📡 Fetching M3U from: {url}")
            
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url)
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
            logger.error(f"❌ Error: {type(e).__name__}: {e}")
            return None
    
    def parse_m3u_with_ipytv(self, url):
        """Parse M3U using professional ipytv library"""
        if self.loading:
            logger.info("⏳ Already loading")
            return False
        
        self.loading = True
        
        try:
            # Fetch content
            content = self.fetch_m3u_with_httpx(url)
            
            if not content:
                logger.error("❌ Failed to fetch content")
                return False
            
            # Save to temporary file
            temp_file = '/tmp/playlist.m3u'
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Load with ipytv - handles errors automatically
            logger.info("🔄 Parsing with IPyTV...")
            m3u_playlist = playlist.loadf(temp_file)
            
            if not m3u_playlist or len(m3u_playlist) == 0:
                logger.error("❌ No channels found")
                return False
            
            self.playlist_obj = m3u_playlist
            
            # Convert to our format
            channels = {}
            categories = {}
            
            for idx, channel in enumerate(m3u_playlist):
                # Extract channel info
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
                
                # Add to categories
                if category not in categories:
                    categories[category] = []
                categories[category].append(channel_data.copy())
                
                # Add to channels dict
                channel_id = len(channels)
                channels[channel_id] = channel_data
            
            if channels:
                self.channels = channels
                self.categories = categories
                self.last_update = time.time()
                
                logger.info(f"✅ Parsed {len(channels)} channels in {len(categories)} categories")
                
                # Log top 5 categories
                top_cats = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)[:5]
                for cat, ch_list in top_cats:
                    logger.info(f"  📂 {cat}: {len(ch_list)} channels")
                
                return True
            else:
                logger.error("❌ No valid channels")
                return False
                
        except Exception as e:
            logger.error(f"❌ Parse error: {type(e).__name__}: {e}")
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
        logger.info(f"🚀 Starting background M3U load")
        time.sleep(2)  # Give Flask time to start
        try:
            if channel_manager.parse_m3u_with_ipytv(M3U_URL):
                logger.info("✅ Channels loaded successfully")
            else:
                logger.warning("⚠️ Failed to load channels - service continues")
        except Exception as e:
            logger.error(f"❌ Background load error: {e}")
    else:
        logger.warning("⚠️ No M3U_URL configured")

# Start background loading
threading.Thread(target=load_channels_async, daemon=True).start()

@app.route('/')
def index():
    """Home page"""
    return render_template('index.html')

@app.route('/player')
def player():
    """Player page for Mini App"""
    channel_id = request.args.get('ch', type=int)
    
    if channel_id is None:
        return "Channel ID required", 400
    
    channels = channel_manager.get_channels()
    
    if channel_id not in channels:
        return "Channel not found", 404
    
    channel = channels[channel_id]
    
    return render_template('player.html', 
                         channel_name=channel['name'],
                         channel_logo=channel['logo'],
                         channel_id=channel_id)

@app.route('/api/channel/<int:channel_id>')
def get_channel(channel_id):
    """API to get channel stream URL"""
    channels = channel_manager.get_channels()
    
    if channel_id not in channels:
        return jsonify({'error': 'Channel not found'}), 404
    
    channel = channels[channel_id]
    
    return jsonify({
        'name': channel['name'],
        'url': channel['url'],
        'logo': channel['logo'],
        'category': channel['category']
    })

@app.route('/api/channels')
def get_all_channels():
    """API to get all channels"""
    channels = channel_manager.get_channels()
    return jsonify({
        'channels': channels,
        'categories': channel_manager.categories,
        'total': len(channels)
    })

@app.route('/api/reload', methods=['POST'])
def reload_playlist():
    """API to reload M3U playlist"""
    try:
        data = request.get_json() or {}
        url = data.get('url', M3U_URL)
        
        if not url:
            return jsonify({'success': False, 'message': 'No URL provided'}), 400
        
        if channel_manager.parse_m3u_with_ipytv(url):
            return jsonify({
                'success': True, 
                'message': 'Playlist reloaded',
                'channels': len(channel_manager.channels),
                'categories': len(channel_manager.categories)
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to parse'}), 500
    except Exception as e:
        logger.error(f"Reload error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/health')
def health():
    """Health check - always returns OK for Render"""
    return jsonify({
        'status': 'ok',
        'channels': len(channel_manager.channels),
        'categories': len(channel_manager.categories),
        'last_update': channel_manager.last_update,
        'm3u_configured': bool(M3U_URL),
        'loading': channel_manager.loading
    })

@app.route('/api/test-m3u', methods=['POST'])
def test_m3u():
    """Test M3U URL"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'success': False, 'message': 'No URL'}), 400
        
        content = channel_manager.fetch_m3u_with_httpx(url, timeout=15)
        
        if content:
            lines = content.split('\n')[:15]
            return jsonify({
                'success': True,
                'message': 'URL accessible',
                'preview': '\n'.join(lines),
                'size': len(content),
                'lines': len(content.split('\n'))
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to fetch'}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Server error: {e}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info("=" * 60)
    logger.info("🎬 JIO TV BOT - WEB SERVICE")
    logger.info("=" * 60)
    logger.info(f"🌐 Port: {port}")
    logger.info(f"📡 M3U URL: {M3U_URL[:50] + '...' if M3U_URL and len(M3U_URL) > 50 else M3U_URL or 'Not configured'}")
    logger.info(f"🔧 Debug: {debug}")
    logger.info("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=debug)
