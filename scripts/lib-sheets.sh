#!/usr/bin/env bash
# lib-sheets.sh — Funções para Google Sheets API
# Segue o mesmo padrão do yt-pub-lives2
#
# Requer no .env:
#   CLIENT_ID, CLIENT_SECRET, SPREADSHEET_ID
# Requer no config/:
#   credentials.enc, .encryption_key
#
# Uso: source lib-sheets.sh

# --- Configuração ---
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_PROJECT_DIR="$(cd "$LIB_DIR/.." && pwd)"
LIB_CONFIG_DIR="${LIB_PROJECT_DIR}/config"

# Carrega .env se não foi carregado
if [[ -f "${LIB_CONFIG_DIR}/.env" ]]; then
  set -a; source "${LIB_CONFIG_DIR}/.env"; set +a
fi

# --- Funções ---

sheets_get_token() {
  python3 -c "
import json, base64, urllib.request, urllib.parse, os, sys

config_dir = '${LIB_CONFIG_DIR}'

# Decrypt credentials
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
with open(os.path.join(config_dir, '.encryption_key'), 'r') as f:
    key = base64.b64decode(f.read().strip())
with open(os.path.join(config_dir, 'credentials.enc'), 'rb') as f:
    data = f.read()
aesgcm = AESGCM(key)
creds = json.loads(aesgcm.decrypt(data[:12], data[12:], None))

# Refresh token
token_data = urllib.parse.urlencode({
    'client_id': os.environ['CLIENT_ID'],
    'client_secret': os.environ['CLIENT_SECRET'],
    'refresh_token': creds['refresh_token'],
    'grant_type': 'refresh_token'
}).encode()
req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
resp = json.loads(urllib.request.urlopen(req).read())
print(resp['access_token'])
"
}

sheets_get() {
  # Uso: sheets_get "VIDEOS!A1:Z1000"
  local range="$1"
  local token
  token=$(sheets_get_token)
  local encoded_range
  encoded_range=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$range'))")

  curl -s -H "Authorization: Bearer ${token}" \
    "https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}/values/${encoded_range}"
}

sheets_append() {
  # Uso: sheets_append "VIDEOS!A:Z" '[["val1","val2",...]]'
  local range="$1"
  local values="$2"
  local token
  token=$(sheets_get_token)
  local encoded_range
  encoded_range=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$range'))")

  local body
  body=$(python3 -c "
import json
values = json.loads('${values}')
print(json.dumps({
    'range': '${range}',
    'majorDimension': 'ROWS',
    'values': values
}))
")

  curl -s -X POST \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}/values/${encoded_range}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
}

sheets_update() {
  # Uso: sheets_update "VIDEOS!A2:Z2" '[["val1","val2",...]]'
  local range="$1"
  local values="$2"
  local token
  token=$(sheets_get_token)
  local encoded_range
  encoded_range=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$range'))")

  local body
  body=$(python3 -c "
import json
values = json.loads('${values}')
print(json.dumps({
    'range': '${range}',
    'majorDimension': 'ROWS',
    'values': values
}))
")

  curl -s -X PUT \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}/values/${encoded_range}?valueInputOption=RAW"
}

sheets_find_row() {
  # Busca uma linha por valor na coluna A (video_id)
  # Uso: sheets_find_row "VIDEOS" "7616875015815826718"
  # Retorna: número da linha (2, 3, ...) ou vazio se não encontrou
  local sheet="$1"
  local video_id="$2"

  sheets_get "${sheet}!A:A" | python3 -c "
import json, sys
data = json.load(sys.stdin)
values = data.get('values', [])
for i, row in enumerate(values):
    if row and row[0] == '${video_id}':
        print(i + 1)
        sys.exit(0)
" 2>/dev/null
}
