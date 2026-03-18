#!/usr/bin/env bash
# setup.sh — Instala dependências do tktk
set -euo pipefail

echo "==> Instalando dependências Python..."
pip install -r requirements.txt

echo "==> Verificando yt-dlp..."
if ! command -v yt-dlp &>/dev/null; then
  pip install yt-dlp
fi

echo "==> Tornando scripts executáveis..."
chmod +x scripts/tk-*

echo ""
echo "=== Setup completo ==="
echo ""
echo "Próximos passos:"
echo "  1. Copie config/.env.example para config/.env e preencha"
echo "  2. Execute: ./scripts/tk-setup-sheet  (cria a planilha)"
echo "  3. Execute: ./scripts/tk-sync @canal --last 5 --dry-run  (testa)"
echo "  4. Execute: ./scripts/tk-dashboard  (abre dashboard em http://localhost:8092)"
