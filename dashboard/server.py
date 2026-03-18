#!/usr/bin/env python3
"""tktk Dashboard — Gerenciamento de sync TikTok → YouTube/Instagram
Segue o padrão do yt-pub-lives2: Python stdlib HTTP server + Google Sheets API
"""

import http.server
import json
import os
import sys
import base64
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import threading
from datetime import datetime
from pathlib import Path

# --- Config ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.join(PROJECT_DIR, 'config')
VIDEOS_DIR = os.path.join(PROJECT_DIR, 'videos')
SCRIPTS_DIR = os.path.join(PROJECT_DIR, 'scripts')
ENV_FILE = os.path.join(CONFIG_DIR, '.env')
PORT = 8095

# Load env
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val

SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')


def get_access_token():
    """Get OAuth access token from encrypted credentials."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    with open(os.path.join(CONFIG_DIR, '.encryption_key'), 'r') as f:
        key = base64.b64decode(f.read().strip())

    with open(os.path.join(CONFIG_DIR, 'credentials.enc'), 'rb') as f:
        data = f.read()

    aesgcm = AESGCM(key)
    creds = json.loads(aesgcm.decrypt(data[:12], data[12:], None))

    token_data = urllib.parse.urlencode({
        'client_id': os.environ['CLIENT_ID'],
        'client_secret': os.environ['CLIENT_SECRET'],
        'refresh_token': creds['refresh_token'],
        'grant_type': 'refresh_token'
    }).encode()

    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp['access_token']


def sheets_api(method, endpoint, body=None):
    """Call Google Sheets API."""
    token = get_access_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/{endpoint}'

    if body:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
    else:
        req = urllib.request.Request(url, method=method)

    req.add_header('Authorization', f'Bearer {token}')

    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        return {'error': error_body, 'status': e.code}


def sheets_get(range_str):
    encoded_range = urllib.parse.quote(range_str)
    return sheets_api('GET', f'values/{encoded_range}')


def sheets_update(range_str, values):
    encoded_range = urllib.parse.quote(range_str)
    body = {'range': range_str, 'majorDimension': 'ROWS', 'values': values}
    return sheets_api('PUT', f'values/{encoded_range}?valueInputOption=RAW', body)


def sheets_append(range_str, values):
    encoded_range = urllib.parse.quote(range_str)
    body = {'range': range_str, 'majorDimension': 'ROWS', 'values': values}
    return sheets_api('POST', f'values/{encoded_range}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS', body)


# --- API Handlers ---

def api_status():
    """Retorna status geral."""
    # Conta vídeos locais
    videos_path = Path(VIDEOS_DIR)
    local_videos = []
    if videos_path.exists():
        for d in videos_path.iterdir():
            if d.is_dir() and any(d.glob('video.*')):
                summary_file = d / 'summary.json'
                if summary_file.exists():
                    with open(summary_file) as f:
                        local_videos.append(json.load(f))

    # Dados da planilha
    try:
        videos_data = sheets_get('VIDEOS!A2:N1000')
        videos_rows = videos_data.get('values', [])
    except:
        videos_rows = []

    try:
        pub_data = sheets_get('PUBLICADOS!A2:L1000')
        pub_rows = pub_data.get('values', [])
    except:
        pub_rows = []

    # Stats
    total = len(videos_rows)
    downloaded = sum(1 for r in videos_rows if len(r) > 11 and r[11] == 'downloaded')
    analyzed = sum(1 for r in videos_rows if len(r) > 11 and r[11] == 'analyzed')
    published = sum(1 for r in videos_rows if len(r) > 11 and r[11] == 'published')
    pending = total - published

    return {
        'total_videos': total,
        'downloaded': downloaded,
        'analyzed': analyzed,
        'published': published,
        'pending': pending,
        'total_publicados': len(pub_rows),
        'disk_videos': len(local_videos)
    }


def api_videos():
    """Lista todos os vídeos da planilha."""
    data = sheets_get('VIDEOS!A1:N1000')
    rows = data.get('values', [])
    if not rows:
        return []

    headers = rows[0]
    videos = []
    for row in rows[1:]:
        video = {}
        for i, h in enumerate(headers):
            video[h] = row[i] if i < len(row) else ''
        videos.append(video)

    return videos


def api_config():
    """Retorna configuração da planilha."""
    data = sheets_get('CONFIG!A2:B20')
    rows = data.get('values', [])
    config = {}
    for row in rows:
        if len(row) >= 2:
            config[row[0]] = row[1]
    return config


def api_config_update(body):
    """Atualiza configuração na planilha."""
    key = body.get('key')
    value = body.get('value')
    if not key:
        return {'error': 'key required'}

    # Busca a linha da chave
    data = sheets_get('CONFIG!A2:B20')
    rows = data.get('values', [])
    for i, row in enumerate(rows):
        if row and row[0] == key:
            sheets_update(f'CONFIG!B{i+2}', [[value]])
            return {'ok': True, 'key': key, 'value': value}

    # Se não existe, adiciona
    sheets_append('CONFIG!A:B', [[key, value]])
    return {'ok': True, 'key': key, 'value': value, 'created': True}


def api_sync(body):
    """Dispara sync em background."""
    channel = body.get('channel', os.environ.get('TIKTOK_CHANNEL', ''))
    last = body.get('last', '10')
    since = body.get('since', '')
    until = body.get('until', '')
    order = body.get('order', 'newest')

    if not channel:
        return {'error': 'channel required'}

    cmd = [os.path.join(SCRIPTS_DIR, 'tk-sync'), channel, '--last', str(last)]
    if since:
        cmd += ['--since', since]
    if until:
        cmd += ['--until', until]
    if order == 'oldest':
        cmd += ['--order', 'oldest']

    def run_sync():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            status_file = os.path.join(PROJECT_DIR, 'dashboard', 'sync_status.json')
            with open(status_file, 'w') as f:
                json.dump({
                    'finished_at': datetime.now().isoformat(),
                    'returncode': result.returncode,
                    'stdout': result.stdout[-2000:] if result.stdout else '',
                    'stderr': result.stderr[-500:] if result.stderr else ''
                }, f)
        except Exception as e:
            pass

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()

    return {'ok': True, 'message': f'Sync iniciado para {channel} (last {last})', 'pid': 'background'}


def api_sync_status():
    """Retorna status do último sync."""
    status_file = os.path.join(PROJECT_DIR, 'dashboard', 'sync_status.json')
    if os.path.exists(status_file):
        with open(status_file) as f:
            return json.load(f)
    return {'status': 'never_run'}


def api_update_video_status(body):
    """Atualiza status de um vídeo na planilha."""
    video_id = body.get('video_id')
    status = body.get('status')
    if not video_id or not status:
        return {'error': 'video_id and status required'}

    data = sheets_get('VIDEOS!A2:N1000')
    rows = data.get('values', [])
    for i, row in enumerate(rows):
        if row and row[0] == video_id:
            sheets_update(f'VIDEOS!L{i+2}', [[status]])
            return {'ok': True}

    return {'error': 'video not found'}


# --- Prompt ---

PROMPT_FILE = os.path.join(CONFIG_DIR, 'prompt_descricao.txt')


def api_prompt_get():
    """Retorna o prompt de análise."""
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE) as f:
            return {'prompt': f.read()}
    return {'prompt': ''}


def api_prompt_save(body):
    """Salva o prompt de análise."""
    prompt = body.get('prompt', '')
    with open(PROMPT_FILE, 'w') as f:
        f.write(prompt)
    return {'ok': True}


# --- Analyze ---

def api_analyze(body):
    """Dispara análise de um vídeo em background."""
    video_id = body.get('video_id')
    if not video_id:
        return {'error': 'video_id required'}

    cmd = [os.path.join(SCRIPTS_DIR, 'tk-analyze'), video_id]

    def run_analyze():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            status_file = os.path.join(PROJECT_DIR, 'dashboard', 'analyze_status.json')
            with open(status_file, 'w') as f:
                json.dump({
                    'video_id': video_id,
                    'finished_at': datetime.now().isoformat(),
                    'returncode': result.returncode,
                    'stdout': result.stdout[-2000:] if result.stdout else '',
                    'stderr': result.stderr[-500:] if result.stderr else ''
                }, f)
        except Exception as e:
            pass

    thread = threading.Thread(target=run_analyze, daemon=True)
    thread.start()

    return {'ok': True, 'message': f'Análise iniciada para {video_id}'}


def api_publish(body):
    """Dispara publicação de um vídeo em background."""
    video_id = body.get('video_id')
    platform = body.get('platform', 'youtube')
    privacy = body.get('privacy', '')
    if not video_id:
        return {'error': 'video_id required'}

    cmd = [os.path.join(SCRIPTS_DIR, 'tk-publish'), video_id, '--platform', platform]
    if privacy:
        cmd += ['--privacy', privacy]

    def run_publish():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            status_file = os.path.join(PROJECT_DIR, 'dashboard', 'publish_status.json')
            with open(status_file, 'w') as f:
                json.dump({
                    'video_id': video_id,
                    'finished_at': datetime.now().isoformat(),
                    'returncode': result.returncode,
                    'stdout': result.stdout[-2000:] if result.stdout else '',
                    'stderr': result.stderr[-500:] if result.stderr else ''
                }, f)
        except Exception as e:
            pass

    thread = threading.Thread(target=run_publish, daemon=True)
    thread.start()

    return {'ok': True, 'message': f'Publicação iniciada para {video_id} ({platform})'}


# --- HTTP Server ---

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.path = '/index.html'
            return super().do_GET()

        if self.path.startswith('/api/'):
            return self.handle_api_get()

        return super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/'):
            return self.handle_api_post()
        self.send_error(404)

    def handle_api_get(self):
        routes = {
            '/api/status': api_status,
            '/api/videos': api_videos,
            '/api/config': api_config,
            '/api/sync-status': api_sync_status,
            '/api/prompt': api_prompt_get,
            '/api/scheduler-status': lambda: json.load(open(os.path.join(PROJECT_DIR, 'dashboard', 'scheduler_status.json'))) if os.path.exists(os.path.join(PROJECT_DIR, 'dashboard', 'scheduler_status.json')) else {'state': 'stopped'},
        }
        handler = routes.get(self.path)
        if handler:
            try:
                result = handler()
                self.send_json(result)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
        else:
            self.send_error(404)

    def handle_api_post(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = {}
        if content_length > 0:
            body = json.loads(self.rfile.read(content_length))

        routes = {
            '/api/sync': api_sync,
            '/api/config': api_config_update,
            '/api/video/status': api_update_video_status,
            '/api/prompt': api_prompt_save,
            '/api/analyze': api_analyze,
            '/api/publish': api_publish,
        }
        handler = routes.get(self.path)
        if handler:
            try:
                result = handler(body)
                self.send_json(result)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
        else:
            self.send_error(404)

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        if '/api/' in str(args[0]):
            sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}\n")


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"tktk Dashboard: http://localhost:{port}")
    server = http.server.HTTPServer(('0.0.0.0', port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard encerrado.")
