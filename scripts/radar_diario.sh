#!/usr/bin/env bash
# Corrida diaria del radar. Solo lectura: no inicia sesión, no llena formularios, no envía nada.
# Linux (cron, 4:00 a. m.):  0 4 * * * /ruta/al/repo/scripts/radar_diario.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/bin:/bin"
mkdir -p salida/logs
LOG="salida/logs/$(date +%Y-%m-%d).log"
SEG=()
[[ -f config/seguimiento.md ]] && SEG=(--seguimiento config/seguimiento.md)
if uv run radar buscar "${SEG[@]}" >>"$LOG" 2>&1; then
  # Sin "|| true", un log sin la línea "Top" cortaría el script por pipefail.
  MSG="$(grep -E '^Top ' "$LOG" | tail -1 || true)"
  MSG="${MSG:-Corrida terminada: revise $LOG}"
  status="ok"
else
  MSG="La corrida falló: revise $LOG"
  status="falla"
fi
# Notificación local en macOS; en Linux no hace nada.
command -v osascript >/dev/null && osascript -e "display notification \"${MSG//\"/}\" with title \"Radar de vacantes\"" || true
[[ $status == ok ]]
