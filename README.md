# GptPlatform

Estudio local de generación y edición de **imágenes y audio** con la API de OpenAI
(`gpt-image-2`, `gpt-image-1` para fondo transparente, `gpt-4o-mini-tts`/`tts-1` para voz
y `gpt-4o-transcribe`/`whisper-1` para transcripción). Una sola página servida
por un script de Python sin dependencias externas. La idea: hacer en local, con
buena UX, todo lo que permite OpenAI Platform, y sumar **memoria de estilo por proyecto**.

![tema oscuro · minimalista](#)

## Qué hace

- **Crear** (texto → imagen) y **Editar / combinar** (sube imágenes de referencia + prompt).
- **Editor de imagen integrado con 3 pestañas**: **Máscara** (pincel, borrador, rectángulo, lazo — o sube tu PNG), **Anotar** (flechas, círculos, trazo libre y texto en rojo: el modelo sigue las instrucciones dibujadas sin incluirlas en el resultado) y **Pins** (marcadores numerados con una instrucción por punto que se añade sola al prompt).
- **Cantidad 1–4 con resultados múltiples**: todas las imágenes se muestran en una tira, se guardan y se cobran bien (antes solo se conservaba la primera).
- **Pegar con ⌘V** y **arrastrar a cualquier parte** (archivos del Mac o miniaturas del propio historial) para añadir referencias.
- **Tamaño libre** con sliders + presets de aspect ratio (social, foto, **cine / anamórfico**) con miniaturas, candado de proporción, y **chips de resolución (1080 / 2K / 3K / 4K)** que escalan el ratio actual manteniendo la proporción.
- **Calidad, formato (PNG/JPG/WebP), compresión, moderación**.
- **Fondo transparente** (cambia automáticamente a `gpt-image-1`).
- **Estimador de costo** antes de generar + **costo real** (tokens) después + total de sesión.
- **Ancho/alto editables con el teclado**: clic en el número, escribe y Enter (ajusta solo a múltiplos de 16).
- **Historial pro**: filtro por proyecto, "ver más", borrar (doble clic en la papelera), clic abre lightbox con prompt y descarga, arrastrar al panel de referencias.
- **Memoria de proyecto**: `estilo.md` (texto que se antepone) + **referencias visuales** que se adjuntan solas al generar. Botón para **destilar el estilo** con IA.
- **Carpeta de guardado configurable** (por defecto el Escritorio) o sin copia extra; el destino se muestra siempre bajo el botón Generar.
- **Memoria visual con drag & drop**: arrastra imágenes (del Mac o del historial) directo a "Añadir referencia" del proyecto.
- **Sección Audio** (modo `3` en la barra): **Voz (TTS)** con `gpt-4o-mini-tts` (instrucciones de tono libres), `tts-1-hd` y `tts-1` (velocidad 0.25–4×), 11 voces con vista previa, 6 formatos (MP3/WAV/AAC/FLAC/Opus/PCM), contador 0/4096 y estimador de costo. **Transcripción** con `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` y `whisper-1`: idioma, contexto, temperatura, salida en texto/SRT/VTT/JSON con tiempos, y **traducción al inglés**. Historial de audio con reproductor inline, descarga y borrado; arrastra cualquier audio a la ventana y se carga solo para transcribir.
- **Estilos de voz guardados**: combinaciones voz + instrucciones de tono con nombre propio ("Narrador docu", "Promo bar"…) que se aplican con un clic; persisten en `config.json`.
- **ElevenLabs como segundo proveedor de voz** (clave en `~/.elevenlabs_key`, plan gratis disponible): voces de tu cuenta en vivo **incluidas las clonadas**, modelos Multilingual v2 / Eleven v3 / Turbo v2.5 / Flash v2.5, ajustes completos (estabilidad, similitud, exageración de estilo, velocidad 0.7–1.2×, speaker boost), seed reproducible, normalización de texto, 6 formatos de salida, **cuota de créditos visible** (ElevenLabs sí expone el saldo), vista previa, **clonación de voz** (IVC, sube muestras y la voz aparece en la lista) y pestaña de **efectos de sonido** (texto → SFX de hasta 22 s con duración y apego al prompt).
- **Sección Video** (modo `4`, vía fal.ai con una sola clave en `~/.fal_key`) con **tres pestañas independientes y la API completa de cada modelo**:
  - **Seedance 2.0** (Estándar/Fast): texto→video, imagen→video con **frame final opcional**, y **modo referencia multimodal** (hasta 9 imágenes + 3 videos de estilo/movimiento + 3 audios guía, máx 12 archivos) para mantener personajes y estética entre tomas. 480p–1080p, duración auto/4–15s, 7 aspectos, audio nativo, seed.
  - **Kling 3.0** (Pro/Standard, este ~2.6× más barato): prompt único o **multi-toma** (varias escenas con duración por toma, formato "texto | segundos") con estructura customize/intelligent, imagen inicial **y final**, 3–15s, 3 aspectos, audio nativo es/en, prompt negativo y CFG.
  - **Google Veo 3.1**: texto→video e imagen→video con audio nativo (diálogos, música, ambiente), 4s/6s/8s, **720p/1080p/4K**, 16:9 o 9:16, prompt negativo, auto-fix y seed. $0.20–0.60/s según resolución y audio.
  - **OmniHuman** (1.5/1.0): avatar que habla a partir de imagen + audio (elegible directo del historial de voces generadas), con indicaciones de texto, turbo y 720p/1080p en la 1.5. $0.14/s.

  Generación asíncrona con estado de cola en vivo; el MP4 se descarga solo al historial y a tu carpeta, con su sección de Video en el panel derecho (reproducir, descargar, borrar).
- **Atajos**: `⌘↵` genera, `1`/`2`/`3`/`4` cambia Crear/Editar/Audio/Video, `Esc` cierra modales y lightbox.
- **Notificaciones toast** y errores legibles con botón de reintento.
- Pantalla para **conectar tu API key** desde el navegador (se guarda en `~/.openai_key`), con indicador de conexión en la barra.

## Requisitos

- macOS o Linux con **Python 3** (sin librerías extra).
- Una **API key de OpenAI** (https://platform.openai.com/api-keys).

## Uso

```bash
python3 server.py
```

Abre http://localhost:7860 y pulsa **API** para conectar tu clave. Listo.

En macOS puedes hacer **doble clic** en `Estudio.command` (arranca el server y abre el navegador).

## Notas

- **Nada se borra al apagar**: historial, proyectos, estilos y configuración viven en `~/image-studio/` y sobreviven reinicios. Los JSON se escriben de forma atómica con respaldo `.bak` y se auto-recuperan si se corrompen. El estilo de cada proyecto se guarda además como archivo real `estilo.md` en `~/image-studio/proyectos/<nombre>/` (puedes editarlo a mano; la app lo lee). El proyecto seleccionado se recuerda entre sesiones.
- La clave vive solo en tu equipo (`~/.openai_key`), nunca en el repo.
- **Seguridad**: el server solo escucha en `127.0.0.1` y además valida el header `Host` (anti DNS-rebinding) y el `Origin` de todo POST (anti-CSRF: una web maliciosa no puede disparar generaciones ni borrados contra tu app local). La página se sirve con CSP estricta, `X-Frame-Options: DENY`, `nosniff` y `Referrer-Policy: no-referrer`. Claves con permisos 600, escrituras JSON atómicas con lock y respaldo, límite de 256MB por petición y nombres de archivo saneados en todo lo que sube a las APIs.
- Las imágenes generadas se guardan en `~/image-studio/historial/` y, si lo activas, una copia en la carpeta que elijas (Escritorio por defecto; se configura en Ajustes avanzados y se persiste en `~/image-studio/config.json`).
- `historial/`, `proyectos/` y los `.json` son datos locales y están en `.gitignore`.

## Estructura

| Archivo | Qué es |
|---|---|
| `server.py` | La app completa (backend + UI embebida) |
| `Estudio.command` | Lanzador de doble clic (macOS) |
| `PRODUCT.md` | Contexto de producto (registro, usuarios, principios) |
| `DESIGN.md` | Sistema visual (tokens, tipografía, componentes) |

## Límites de gpt-image-2 (referencia)

- Ancho y alto múltiplos de 16 · lado más largo ≤ 3840 · mínimo ~0.8 MP.
- Referencias de edición ≤ 50 MB c/u (PNG/JPG/WebP).
- Fondo transparente solo en `gpt-image-1`.
- Moderación: `auto` o `low` (OpenAI siempre modera; no hay modo sin filtro).

## Sincronizar entre Macs (iCloud Drive)

Los datos viven en `~/image-studio/`. Para compartir sesiones, historial y memorias
entre tus Macs, doble clic en **`Sincronizar iCloud.command`**: mueve la carpeta a
iCloud Drive y deja un symlink en su lugar (la app no nota la diferencia). En el otro
Mac: clona el repo, ejecuta el mismo script y conecta tus claves con el botón API.
Si el otro Mac tenía datos propios, se respaldan en `~/image-studio-backup-<fecha>/`.
