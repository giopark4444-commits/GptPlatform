#!/bin/bash
# Sincroniza los datos del Studio (~/image-studio) vía iCloud Drive en este Mac.
# Doble clic y listo: sesiones, historial, proyectos y memorias compartidos entre tus Macs.

ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
TARGET="$ICLOUD/image-studio"
LOCAL="$HOME/image-studio"

echo "── Studio · sincronización por iCloud Drive ──"

if [ ! -d "$ICLOUD" ]; then
  echo "✗ iCloud Drive no está activo en este Mac."
  echo "  Actívalo en Ajustes del Sistema → Apple ID → iCloud → iCloud Drive."
  exit 1
fi

if [ -L "$LOCAL" ]; then
  echo "✓ Ya estaba configurado: ~/image-studio apunta a iCloud."
  exit 0
fi

if [ -d "$TARGET" ]; then
  # iCloud ya trae los datos (del otro Mac)
  if [ -d "$LOCAL" ]; then
    BACKUP="$HOME/image-studio-backup-$(date +%Y%m%d_%H%M)"
    mv "$LOCAL" "$BACKUP"
    echo "• Este Mac tenía datos propios: respaldados en $BACKUP"
    echo "  (si quieres fusionarlos, copia a mano lo que te interese)"
  fi
  echo "• Usando los datos que ya están en iCloud."
else
  if [ -d "$LOCAL" ]; then
    mv "$LOCAL" "$TARGET"
    echo "• Datos locales movidos a iCloud Drive."
  else
    mkdir -p "$TARGET"
    echo "• Carpeta nueva creada en iCloud Drive."
  fi
fi

ln -s "$TARGET" "$LOCAL"
echo "✓ Listo: ~/image-studio → iCloud Drive."
echo "  Historial, proyectos, estilos y configuración se sincronizan solos."
echo "  Recuerda: las claves (~/.openai_key, ~/.elevenlabs_key) se conectan"
echo "  una vez por Mac desde el botón API de la app."
