from flask import Flask, render_template, request, jsonify
import os
import threading
import logging
from ipytv import playlist
from ipytv.playlist import M3UPlaylist, IPTVAttr
import httpx
import requests
import time
from urllib.parse import urlparse

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
    
    def fetch_m3u_with_requests(self, url, timeout=90):
        """Fetch M3U using requests library (more reliable for some servers)"""
        try:
            logger.info(f"📡 [Method: requests] Fetching M3U from: {url[:60]}...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Referer': f'{urlparse(url).scheme}://{urlparse(url).netloc}/'
            }
            
            session = requests.Session()
            session.max_redirects = 10
            
            response = session.get(
                url,
                headers=headers,
                timeout=timeout,
                stream=False,
                allow_redirects=True,
                verify=True
            )
            
            response.raise_for_status()
            
            # Handle different encodings
            if response.encoding is None:
                response.encoding = 'utf-8'
            
            content = response.text
            
            if not content.strip():
                logger.error("❌ Empty response received")
                return None
            
            # Validate M3U format
            if not content.strip().startswith('#EXTM3U'):
                logger.warning("⚠️ Content doesn't start with #EXTM3U, but attempting to parse...")
            
            logger.info(f"✅ Successfully fetched {len(content)} bytes ({len(content.splitlines())} lines)")
            return content
            
        except requests.exceptions.Timeout:
            logger.error(f"⏱️ Timeout after {timeout}s using requests")
            return None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Connection error: {str(e)[:100]}")
            return None
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ HTTP Error {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"❌ Requests error: {type(e).__name__}: {str(e)[:100]}")
            return None
    
    def fetch_m3u_with_httpx(self, url, timeout=90):
        """Fetch M3U using httpx library (alternative method)"""
        try:
            logger.info(f"📡 [Method: httpx] Fetching M3U from: {url[:60]}...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive'
            }
            
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=30.0),
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                headers=headers,
                verify=True
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                content = response.text
                
                if not content.strip():
                    logger.error("❌ Empty response")
                    return None
                
                logger.info(f"✅ Fetched {len(content)} bytes ({len(content.splitlines())} lines)")
                return content
                
        except httpx.TimeoutException:
            logger.error(f"⏱️ Timeout after {timeout}s using httpx")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"❌ Httpx error: {type(e).__name__}: {str(e)[:100]}")
            return None
    
    def fetch_m3u_smart(self, url):
        """Smart fetcher with multiple fallback strategies"""
        logger.info("🔍 Starting smart M3U fetch with multiple strategies...")
        
        # Strategy 1: Try requests first (most reliable for PHP endpoints)
        logger.info("📥 Strategy 1: Using requests library...")
        content = self.fetch_m3u_with_requests(url, timeout=90)
        if content:
            logger.info("✅ Strategy 1 succeeded!")
            return content
        
        logger.warning("⚠️ Strategy 1 failed, trying alternative...")
        time.sleep(2)
        
        # Strategy 2: Try httpx
        logger.info("📥 Strategy 2: Using httpx library...")
        content = self.fetch_m3u_with_httpx(url, timeout=90)
        if content:
            logger.info("✅ Strategy 2 succeeded!")
            return content
        
        logger.warning("⚠️ Strategy 2 failed, trying with extended timeout...")
        time.sleep(3)
        
        # Strategy 3: Extended timeout with requests
        logger.info("📥 Strategy 3: Extended timeout (120s) with requests...")
        content = self.fetch_m3u_with_requests(url, timeout=120)
        if content:
            logger.info("✅ Strategy 3 succeeded!")
            return content
        
        logger.error("❌ All fetch strategies failed!")
        return None
    
    def parse_m3u_with_ipytv(self, url):
        """Parse M3U using professional ipytv library"""
        if self.loading:
            logger.info("⏳ Already loading - skipping")
            return False
        
        self.loading = True
        start_time = time.time()
        
        try:
            # Fetch content with smart strategy
            logger.info("="*60)
            logger.info("🚀 Starting M3U download and parse operation")
            logger.info("="*60)
            
            content = self.fetch_m3u_smart(url)
            
            if not content:
                logger.error("❌ Failed to fetch content after trying all strategies")
                return False
            
            # Validate content
            lines = content.splitlines()
            logger.info(f"📊 Content stats: {len(content)} bytes, {len(lines)} lines")
            
            # Count #EXTINF entries (channels)
            extinf_count = sum(1 for line in lines if line.strip().startswith('#EXTINF'))
            logger.info(f"📺 Found {extinf_count} potential channel entries (#EXTINF)")
            
            # Save to temporary file
            temp_file = '/tmp/playlist.m3u'
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            logger.info(f"💾 Saved to {temp_file} ({os.path.getsize(temp_file)} bytes)")
            
            # Load with ipytv - handles errors automatically
            logger.info("🔄 Parsing with IPyTV library...")
            m3u_playlist = playlist.loadf(temp_file)
            
            if not m3u_playlist or len(m3u_playlist) == 0:
                logger.error("❌ IPyTV returned empty playlist")
                # Try to show first few lines for debugging
                preview = '\n'.join(lines[:10])
                logger.error(f"📄 File preview:\n{preview}")
                return False
            
            self.playlist_obj = m3u_playlist
            logger.info(f"✅ IPyTV parsed {len(m3u_playlist)} entries")
            
            # Convert to our format
            channels = {}
            categories = {}
            skipped = 0
            
            for idx, channel in enumerate(m3u_playlist):
                # Extract channel info
                name = channel.name or f'Channel {idx + 1}'
                logo = channel.attributes.get(IPTVAttr.TVG_LOGO.value, '')
                category = channel.attributes.get(IPTVAttr.GROUP_TITLE.value, 'Uncategorized')
                tvg_id = channel.attributes.get(IPTVAttr.TVG_ID.value, '')
                url = channel.url or ''
                
                # Skip channels without URL
                if not url or url == '':
                    skipped += 1
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
                
                elapsed = time.time() - start_time
                
                logger.info("="*60)
                logger.info(f"✅ PARSING COMPLETE!")
                logger.info(f"📺 Valid channels: {len(channels)}")
                logger.info(f"📂 Categories: {len(categories)}")
                logger.info(f"⏱️ Time taken: {elapsed:.2f}s")
                if skipped > 0:
                    logger.info(f"⚠️ Skipped entries: {skipped} (no URL)")
                logger.info("="*60)
                
                # Log top 10 categories
                top_cats = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)[:10]
                logger.info("📊 Top 10 categories:")
                for cat, ch_list in top_cats:
                    logger.info(f"  📂 {cat}: {len(ch_list)} channels")
                logger.info("="*60)
                
                return True
            else:
                logger.error("❌ No valid channels after parsing")
                return False
                
        except FileNotFoundError as e:
            logger.error(f"❌ File error: {e}")
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
        logger.info(f"🔗 URL: {M3U_URL}")
        time.sleep(3)  # Give Flask time to start properly
        try:
            if channel_manager.parse_m3u_with_ipytv(M3U_URL):
                logger.info("✅✅✅ Background load completed successfully!")
            else:
                logger.warning("⚠️⚠️⚠️ Failed to load channels - service continues without channels")
                logger.warning("💡 Possible issues:")
                logger.warning("   - URL might be slow or temporarily unavailable")
                logger.warning("   - Network connectivity issues")
                logger.warning("   - Server blocking requests")
        except Exception as e:
            logger.error(f"❌ Background load error: {e}")
            import traceback
            logger.error(traceback.format_exc())
    else:
        logger.warning("⚠️ No M3U_URL configured - set M3U_URL environment variable")

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
        <h1>🎬 IPTV Service</h1>
        <p>Channels: {len(channel_manager.channels)}</p>
        <p>Categories: {len(channel_manager.categories)}</p>
        <p>Status: {'Loading...' if channel_manager.loading else 'Ready'}</p>
        """, 200

@app.route('/player')
def player():
    """Player page for Mini App"""
    channel_id = request.args.get('ch', type=int)
    
    if channel_id is None:
        return "Channel ID required", 400
    
    channels = channel_manager.get_channels()
    
    if channel_id not in channels:
        logger.warning(f"Channel {channel_id} not found")
        return "Channel not found", 404
    
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
        'total': len(channels),
        'last_update': channel_manager.last_update
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
        
        if channel_manager.parse_m3u_with_ipytv(url):
            return jsonify({
                'success': True, 
                'message': 'Playlist reloaded successfully',
                'channels': len(channel_manager.channels),
                'categories': len(channel_manager.categories)
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to parse M3U'}), 500
    except Exception as e:
        logger.error(f"Reload error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/health')
def health():
    """Health check - always returns OK for Render"""
    return jsonify({
        'status': 'healthy',
        'channels_loaded': len(channel_manager.channels),
        'categories': len(channel_manager.categories),
        'last_update': channel_manager.last_update,
        'last_update_ago': f"{int(time.time() - channel_manager.last_update)}s" if channel_manager.last_update > 0 else "never",
        'm3u_configured': bool(M3U_URL),
        'm3u_url': M3U_URL[:50] + '...' if M3U_URL and len(M3U_URL) > 50 else M3U_URL,
        'loading': channel_manager.loading,
        'uptime': int(time.time())
    }), 200

@app.route('/api/test-m3u', methods=['POST'])
def test_m3u():
    """Test M3U URL"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'success': False, 'message': 'No URL provided'}), 400
        
        logger.info(f"🧪 Testing M3U URL: {url[:60]}...")
        content = channel_manager.fetch_m3u_smart(url)
        
        if content:
            lines = content.split('\n')
            preview_lines = [line for line in lines[:25] if line.strip()]
            extinf_count = sum(1 for line in lines if line.strip().startswith('#EXTINF'))
            
            return jsonify({
                'success': True,
                'message': 'URL is accessible',
                'preview': '\n'.join(preview_lines),
                'size': len(content),
                'total_lines': len(lines),
                'non_empty_lines': len([l for l in lines if l.strip()]),
                'channel_count_estimate': extinf_count
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to fetch M3U from URL after all strategies'}), 500
            
    except Exception as e:
        logger.error(f"Test M3U error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Server error: {e}")
    return jsonify({'error': 'Internal server error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info("=" * 70)
    logger.info("🎬 JIO TV BOT - WEB SERVICE")
    logger.info("=" * 70)
    logger.info(f"🌐 Port: {port}")
    logger.info(f"📡 M3U URL: {M3U_URL if M3U_URL else '❌ Not configured'}")
    logger.info(f"🔧 Debug Mode: {debug}")
    logger.info(f"📁 Templates exist: {os.path.exists('templates')}")
    logger.info("=" * 70)
    logger.info("🚀 Starting Flask server...")
    logger.info("💡 Channels will load in background (non-blocking)")
    logger.info("🔄 Using smart multi-strategy fetcher (requests + httpx)")
    logger.info("=" * 70)
    
    app.run(host='0.0.0.0', port=port, debug=debug)
