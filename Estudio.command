#!/bin/bash
# Lanzador del Estudio v2.1 — doble clic para abrir
if ! curl -s http://localhost:7860/ >/dev/null 2>&1; then
  nohup python3 "$HOME/image-studio/server.py" >"$HOME/image-studio/studio.log" 2>&1 &
  sleep 2
fi
open http://localhost:7860
