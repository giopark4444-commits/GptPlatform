#!/bin/bash
# Sincroniza tus datos del Studio (historial, proyectos, config) con GitHub.
# En este Mac: respalda y comparte. En un Mac NUEVO: descarga todo.
REPO="https://github.com/giopark4444-commits/studio-datos.git"
D="$HOME/image-studio"
if [ -d "$D/.git" ]; then
  cd "$D"
  git add -A; git -c user.email=gio.park.4444@gmail.com -c user.name=Gio commit -m "sync $(date '+%Y-%m-%d %H:%M')" 2>/dev/null
  git pull --no-rebase --no-edit origin main && git push origin main && echo "✓ Datos sincronizados."
elif [ -d "$D" ]; then
  echo "Ya hay datos locales sin git. Haz respaldo y avísame para fusionar."
else
  git clone "$REPO" "$D" && echo "✓ Datos descargados en este Mac."
fi
