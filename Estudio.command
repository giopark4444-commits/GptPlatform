#!/bin/bash
# Estudio — doble clic: arranca el server (si no corre) y abre el navegador
if ! curl -s -o /dev/null --max-time 1 http://localhost:7860/keystatus; then
  nohup /usr/bin/python3 "$HOME/GptPlatform/server.py" > /tmp/studio.log 2>&1 &
  sleep 1.5
fi
open "http://localhost:7860"
