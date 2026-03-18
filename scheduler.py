#!/usr/bin/env python3
"""
tktk Scheduler — Automação de sync/analyze/publish
Roda em loop, checa a cada minuto se está na hora de executar.
Lê configuração da planilha CONFIG.
"""

import json
import os
import sys
import time
import subprocess
import base64
import urllib.request
import urllib.parse
import threading
from datetime import datetime

# Config
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(PROJECT_ROOT, 'config')
ENV_FILE = os.path.join(CONFIG_DIR, '.env')
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, 'scripts')
STATUS_FILE = os.path.join(PROJECT_ROOT, 'dashboard', 'scheduler_status.json')

# Load env
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val

SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', file=sys.stderr, flush=True)


def update_status(state, detail='', step=''):
    """Escreve status atual para o dashboard."""
    data = {
        'state': state,      # idle | syncing | analyzing | publishing | erro
        'detail': detail,
        'step': step,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def get_access_token():
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


def sheets_get(range_str):
    token = get_access_token()
    encoded = urllib.parse.quote(range_str)
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{encoded}'
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode(), 'status': e.code}


def load_config():
    """Lê CONFIG da planilha."""
    result = sheets_get('CONFIG!A1:B30')
    rows = result.get('values', [])
    config = {}
    for row in rows[1:]:
        if len(row) >= 2:
            config[row[0]] = row[1]
    return config


def get_matching_schedule(horarios_str):
    """Retorna o horário que bate com agora (HH:MM), ou None."""
    if not horarios_str:
        return None
    now_hm = datetime.now().strftime('%H:%M')
    for h in horarios_str.split(','):
        h = h.strip()
        if h == now_hm:
            return h
    return None


def run_script(script_name, args, timeout=600):
    """Executa um script e retorna (returncode, stdout, stderr)."""
    cmd = [os.path.join(SCRIPTS_DIR, script_name)] + args
    env = os.environ.copy()
    env['PATH'] = f"{os.path.expanduser('~/.local/bin')}:{os.path.expanduser('~/.deno/bin')}:/usr/bin:{SCRIPTS_DIR}:{env.get('PATH', '')}"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, '', 'timeout'
    except Exception as e:
        return -1, '', str(e)


# --- Pipelines ---

def pipeline_sync(config):
    """Sincroniza novos vídeos do TikTok."""
    channel = config.get('tiktok_channel', '')
    max_per_run = config.get('sync_max_por_vez', '10')

    if not channel:
        log('  Sync: canal não configurado')
        return

    log(f'  Sync: {channel} (max {max_per_run})')
    update_status('syncing', f'Sincronizando {channel}...', 'sync')

    rc, stdout, stderr = run_script('tk-sync', [channel, '--last', max_per_run])

    if rc == 0:
        # Conta novos
        for line in stdout.split('\n'):
            if 'Novos:' in line:
                log(f'  Sync: {line.strip()}')
    else:
        log(f'  Sync: erro (rc={rc})')
        if stderr:
            log(f'  {stderr[:200]}')


def pipeline_analyze(config):
    """Analisa vídeos baixados."""
    log('  Analyze: processando vídeos downloaded...')
    update_status('analyzing', 'Analisando vídeos...', 'analyze')

    rc, stdout, stderr = run_script('tk-analyze', ['--all'], timeout=300)

    if rc == 0:
        for line in stdout.split('\n'):
            if 'analisados' in line.lower():
                log(f'  Analyze: {line.strip()}')
    else:
        log(f'  Analyze: erro (rc={rc})')
        if stderr:
            log(f'  {stderr[:200]}')


def pipeline_publish(config):
    """Publica vídeos analisados."""
    max_per_run = config.get('pub_max_por_vez', '3')
    privacy = config.get('privacy_padrao', 'unlisted')

    log(f'  Publish: max {max_per_run}, privacy={privacy}')
    update_status('publishing', 'Publicando vídeos...', 'publish')

    # Busca vídeos analyzed da planilha
    result = sheets_get('VIDEOS!A2:L1000')
    rows = result.get('values', [])
    analyzed_ids = [r[0] for r in rows if len(r) > 11 and r[11] == 'analyzed']

    count = 0
    for vid in analyzed_ids[:int(max_per_run)]:
        log(f'  Publish: {vid}')
        rc, stdout, stderr = run_script('tk-publish', [vid, '--privacy', privacy])
        if rc == 0:
            count += 1
            for line in stdout.split('\n'):
                if 'youtube.com' in line or 'shorts' in line:
                    log(f'    {line.strip()}')
        else:
            log(f'    Publish erro (rc={rc})')

    log(f'  Publish: {count}/{len(analyzed_ids[:int(max_per_run)])} publicados')


# --- Tracking de execuções (evita rodar 2x no mesmo minuto) ---
executed_today = {
    'sync': set(),
    'pub': set()
}
last_date = datetime.now().strftime('%Y-%m-%d')


def main_loop():
    global executed_today, last_date

    log('tktk Scheduler iniciado')
    update_status('idle', 'Aguardando...')

    while True:
        try:
            # Reset diário
            today = datetime.now().strftime('%Y-%m-%d')
            if today != last_date:
                executed_today = {'sync': set(), 'pub': set()}
                last_date = today

            # Carrega config
            config = load_config()

            now_hm = datetime.now().strftime('%H:%M')

            # --- Pipeline Sync ---
            sync_paused = config.get('pipeline_sync_paused', 'true') == 'true'
            sync_auto = config.get('sync_auto', 'false') == 'true'

            if not sync_paused and sync_auto:
                sync_horarios = config.get('sync_horarios', '')
                match = get_matching_schedule(sync_horarios)
                if match and match not in executed_today['sync']:
                    executed_today['sync'].add(match)
                    log(f'==> Sync agendado ({match})')
                    pipeline_sync(config)
                    # Após sync, analisa automaticamente
                    pipeline_analyze(config)

            # --- Pipeline Publish ---
            pub_paused = config.get('pipeline_pub_paused', 'true') == 'true'
            pub_auto = config.get('pub_auto', 'false') == 'true'

            if not pub_paused and pub_auto:
                pub_horarios = config.get('pub_horarios', '')
                match = get_matching_schedule(pub_horarios)
                if match and match not in executed_today['pub']:
                    executed_today['pub'].add(match)
                    log(f'==> Publish agendado ({match})')
                    pipeline_publish(config)

            update_status('idle', f'Próximo check: {now_hm}')

        except Exception as e:
            log(f'ERRO no loop: {e}')
            update_status('erro', str(e))

        time.sleep(60)


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        log('Scheduler encerrado.')
        update_status('idle', 'Parado')
