from flask import Flask, render_template, request, jsonify
import os
import requests
import re
import time
from functools import lru_cache
from urllib.parse import urljoin, urlparse
import logging

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
        
    def get_channels(self):
        """Get cached channels"""
        if time.time() - self.last_update > self.cache_duration:
            logger.info("Cache expired, reloading channels")
            if M3U_URL:
                self.parse_m3u(M3U_URL)
        return self.channels.copy()
    
    def fetch_m3u_content(self, url, max_retries=3):
        """Fetch M3U content with retry logic and better error handling"""
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
                
                # Use session for better connection handling
                session = requests.Session()
                session.headers.update(headers)
                
                response = session.get(
                    url, 
                    timeout=30,
                    allow_redirects=True,
                    stream=False
                )
                
                response.raise_for_status()
                
                # Check if response is actually M3U content
                content = response.text
                
                if not content.strip():
                    logger.error(f"Empty response from {url}")
                    continue
                
                # Check for valid M3U content
                if not ('#EXTM3U' in content or '#EXTINF' in content or 'http' in content):
                    logger.warning(f"Response doesn't look like M3U content")
                    # Try to parse anyway, might be plain playlist
                
                logger.info(f"Successfully fetched {len(content)} bytes")
                return content
                
            except requests.exceptions.Timeout:
                logger.error(f"Timeout fetching M3U (attempt {attempt + 1})")
                time.sleep(2 ** attempt)  # Exponential backoff
                
            except requests.exceptions.ConnectionError as e:
                logger.error(f"Connection error: {e} (attempt {attempt + 1})")
                time.sleep(2 ** attempt)
                
            except requests.exceptions.HTTPError as e:
                logger.error(f"HTTP error: {e.response.status_code} (attempt {attempt + 1})")
                if e.response.status_code in [403, 401]:
                    logger.error("Access forbidden - check if URL requires authentication")
                break  # Don't retry on 4xx errors
                
            except Exception as e:
                logger.error(f"Unexpected error fetching M3U: {type(e).__name__}: {e}")
                time.sleep(2 ** attempt)
        
        return None
    
    def parse_m3u(self, url):
        """Parse M3U playlist with support for multiple formats"""
        try:
            content = self.fetch_m3u_content(url)
            
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
                
                if not line or line.startswith('##'):
                    continue
                
                # Handle #EXTINF lines
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
                    
                    # Extract channel name (after last comma)
                    name_match = re.search(r',(.+)$', line)
                    if name_match:
                        name = name_match.group(1).strip()
                    else:
                        # Fallback: use tvg-id or generate name
                        name = tvg_id if tvg_id else f'Channel {line_count}'
                    
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
                    
                    # Validate URL
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
                
                # Handle plain URLs (no #EXTINF)
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
                logger.info(f"Successfully parsed {len(channels)} channels in {len(categories)} categories")
                
                # Log category breakdown
                for cat, ch_list in categories.items():
                    logger.info(f"  - {cat}: {len(ch_list)} channels")
                
                return True
            else:
                logger.error("No channels found in M3U playlist")
                return False
                
        except Exception as e:
            logger.error(f"Error parsing M3U: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

# Initialize channel manager
channel_manager = ChannelManager()

# Load channels on startup
if M3U_URL:
    logger.info(f"Loading channels from: {M3U_URL}")
    if channel_manager.parse_m3u(M3U_URL):
        logger.info("Channels loaded successfully on startup")
    else:
        logger.error("Failed to load channels on startup")
else:
    logger.warning("No M3U_URL provided in environment variables")

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
        
        if channel_manager.parse_m3u(url):
            return jsonify({
                'success': True, 
                'message': 'Playlist reloaded',
                'channels': len(channel_manager.channels),
                'categories': len(channel_manager.categories)
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to parse playlist'}), 500
    except Exception as e:
        logger.error(f"Error reloading playlist: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/health')
def health():
    """Health check endpoint"""
    channels = channel_manager.get_channels()
    return jsonify({
        'status': 'ok',
        'channels': len(channels),
        'categories': len(channel_manager.categories),
        'last_update': channel_manager.last_update,
        'm3u_url_configured': bool(M3U_URL)
    })

@app.route('/api/test-m3u', methods=['POST'])
def test_m3u():
    """Test M3U URL without saving"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'success': False, 'message': 'No URL provided'}), 400
        
        content = channel_manager.fetch_m3u_content(url)
        
        if content:
            lines = content.split('\n')[:20]  # First 20 lines
            return jsonify({
                'success': True,
                'message': 'M3U URL is accessible',
                'preview': '\n'.join(lines),
                'size': len(content)
            })
        else:
            return jsonify({'success': False, 'message': 'Failed to fetch M3U'}), 500
            
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
    
    logger.info(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
