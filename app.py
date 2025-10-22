from flask import Flask, render_template, request, jsonify
import os
import requests
import re
from functools import lru_cache

app = Flask(__name__)

# Configuration
M3U_URL = os.environ.get('M3U_URL', '')

class ChannelManager:
    def __init__(self):
        self.channels = {}
        self.categories = {}
        
    @lru_cache(maxsize=1)
    def get_channels(self):
        """Cache channels for 1 hour"""
        return self.channels.copy()
    
    def parse_m3u(self, url):
        """Parse M3U playlist"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            content = response.text
            
            channels = {}
            categories = {}
            
            lines = content.split('\n')
            current_channel = {}
            
            for i, line in enumerate(lines):
                line = line.strip()
                
                if line.startswith('#EXTINF:'):
                    match = re.search(r'tvg-logo="([^"]*)"', line)
                    logo = match.group(1) if match else ''
                    
                    match = re.search(r'group-title="([^"]*)"', line)
                    category = match.group(1) if match else 'Uncategorized'
                    
                    name_match = re.search(r',(.+)$', line)
                    name = name_match.group(1).strip() if name_match else f'Channel {i}'
                    
                    current_channel = {
                        'name': name,
                        'logo': logo,
                        'category': category,
                        'url': ''
                    }
                
                elif line and not line.startswith('#') and current_channel:
                    current_channel['url'] = line
                    
                    if current_channel['category'] not in categories:
                        categories[current_channel['category']] = []
                    
                    categories[current_channel['category']].append(current_channel.copy())
                    
                    channel_id = len(channels)
                    channels[channel_id] = current_channel.copy()
                    current_channel = {}
            
            self.channels = channels
            self.categories = categories
            self.get_channels.cache_clear()
            
            return True
        except Exception as e:
            print(f"Error parsing M3U: {e}")
            return False

channel_manager = ChannelManager()
if M3U_URL:
    channel_manager.parse_m3u(M3U_URL)

@app.route('/')
def index():
    """Home page"""
    return render_template('index.html')

@app.route('/player')
def player():
    """Player page for Mini App"""
    channel_id = request.args.get('ch', type=int)
    
    if channel_id is None or channel_id not in channel_manager.channels:
        return "Channel not found", 404
    
    channel = channel_manager.channels[channel_id]
    
    return render_template('player.html', 
                         channel_name=channel['name'],
                         channel_logo=channel['logo'],
                         channel_id=channel_id)

@app.route('/api/channel/<int:channel_id>')
def get_channel(channel_id):
    """API to get channel stream URL"""
    if channel_id not in channel_manager.channels:
        return jsonify({'error': 'Channel not found'}), 404
    
    channel = channel_manager.channels[channel_id]
    
    return jsonify({
        'name': channel['name'],
        'url': channel['url'],
        'logo': channel['logo'],
        'category': channel['category']
    })

@app.route('/api/channels')
def get_all_channels():
    """API to get all channels"""
    return jsonify({
        'channels': channel_manager.channels,
        'categories': channel_manager.categories
    })

@app.route('/api/reload', methods=['POST'])
def reload_playlist():
    """API to reload M3U playlist"""
    url = request.json.get('url', M3U_URL)
    
    if channel_manager.parse_m3u(url):
        return jsonify({'success': True, 'message': 'Playlist reloaded'})
    else:
        return jsonify({'success': False, 'message': 'Failed to reload'}), 500

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'channels': len(channel_manager.channels)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
