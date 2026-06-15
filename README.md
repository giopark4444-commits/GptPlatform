# GptPlatform — Estudio local de imagen, audio y video con OpenAI

Estudio web **local** para crear y editar **imágenes** (OpenAI `gpt-image-2`), **voz/transcripción/música**
y **video** — todo desde una sola página servida por un script de Python **sin dependencias externas**
(solo librería estándar). La idea: hacer en tu propio Mac, con buena UX, lo que permite OpenAI Platform
(+ proveedores extra vía fal.ai y ElevenLabs), y que **nada se quede en la nube salvo lo que tú generas**.

- **Código:** este repo (`server.py`, un único archivo) — corre en **http://localhost:7860**.
- **Datos:** `~/image-studio/` (historial, proyectos, estante, config). Las imágenes y claves viven **solo en tu equipo**.
- **Arranque:** doble clic en `Estudio.command`, o `python3 server.py`.

## Estado

| Sección | Estado |
|---|---|
| 🖼️ **Imagen** (crear/editar) | ✅ **Completa y pulida** |
| 🔊 **Audio** (voz/transcripción/música) | Funcional — pendiente de pulido |
| 🎬 **Video** (Seedance/Kling/OmniHuman/LipSync) | Funcional — pendiente de pulido |

---

## 🖼️ Imagen (gpt-image-2) — completa

- **Crear** (texto → imagen) y **Editar / combinar** (sube imágenes de referencia + prompt).
- **Editor integrado de 3 pestañas**: **Máscara** (pincel, borrador, rectángulo, lazo, o sube PNG con alfa),
  **Anotar** (flechas, círculos, trazo libre, texto en rojo que el modelo sigue sin incluir) y
  **Pins** (marcadores numerados con una instrucción por punto).
- **Enter genera** en el campo de prompt (Shift+Enter = salto de línea); también en los demás campos.
- **Tamaños** con sliders + presets de aspecto (Social, Foto, **Cine/anamórfico**) **marcados por validez**:
  🟢 verde lleno = nativo gpt-image-2 (1024², 1536×1024, 1024×1536, sin reescalado), 🟩 verde = válido.
  Chips de resolución por **área**: **720p · HD / 1080p · FHD / 1440p · QHD / 4K · UHD**.
  Validación según los límites reales de gpt-image-2: lados ÷16, ≤3840, ratio ≤3:1, 0.65–8.29 MP (aviso ">2K experimental").
- **Calidad** (low/medium/high/auto), **formato** (PNG/JPG/WebP) + compresión, **moderación** (low por defecto).
- **Costos exactos**: estimador antes de generar (calibrado a la tabla oficial, incl. el ajuste de no-cuadrados),
  costo real por tokens después (texto $5 / imagen de entrada $8 / cacheada $2 por 1M, separados), total de sesión,
  y desglose salida/entrada. **Estimado del lote** con confirmación antes de lanzar.
- **Mejorar prompt con IA** (✨, gpt-4o-mini) · **Describir** (imagen → prompt, visión con `detail`) en historial y estante.
- **🗂️ Mis imágenes (estante local)**: carga/arrastra imágenes propias que se **guardan en tu equipo** (no en OpenAI)
  y quedan siempre a la mano debajo del lienzo; **carpeta configurable**, y por imagen: usar como referencia, describir, descargar, quitar.
- **Carpeta de guardado** con **selector nativo de macOS** (botón "Examinar"), o ruta a mano.
- **Historial pro**: una imagen por fila, búsqueda por prompt, favoritas ★, filtro por proyecto, lightbox,
  upscale 2× (fal), borrar, arrastrar a referencias. **Comparador A/B** a pantalla completa con slider. **Iterar** sobre un resultado.
- **Memoria de estilo por proyecto** (`estilo.md` / `estilo-video.md`) + referencias visuales que se adjuntan solas; destilado con IA.
- **Validación de entrada** según OpenAI: solo PNG/JPEG/WebP/GIF (por bytes), ≤1500 imágenes y ≤512 MB por petición.

## 🎨 Apariencia e idioma (Ajustes)

- **6 temas** (selector en Ajustes, se recuerdan): **3 oscuros** — Carbón, Medianoche, Neón — y **3 claros** — Día, Bruma, Crema.
- **Interfaz en 3 idiomas**: Español · English · Français (cambio al vuelo, se recuerda).

## 🔊 Audio

- **Voz (TTS)**: `gpt-4o-mini-tts` (instrucciones de tono libres), `tts-1-hd`, `tts-1` (0.25–4×), 11 voces con vista previa,
  6 formatos, contador 0/4096, estimador. **Estilos de voz guardados** (voz + tono con nombre).
- **Transcripción**: `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` / `whisper-1`, idioma/contexto/temperatura,
  salida texto/SRT/VTT/JSON con tiempos y traducción al inglés. Arrastra un audio y se carga solo.
- **ElevenLabs** (2º proveedor, `~/.elevenlabs_key`): voces de tu cuenta incl. clonadas, modelos v2/v3/Turbo/Flash,
  ajustes completos, cuota visible, clonación (IVC) y **efectos de sonido** (texto → SFX).
- **Música** (vía fal): **Lyria 2** (instrumental 30s WAV 48k) y **MiniMax Music** (canciones con voz/letra).

## 🎬 Video (vía fal.ai, una sola clave `~/.fal_key`)

- **Seedance 2.0** (Estándar/Fast): t2v, i2v con frame final, y referencia multimodal (hasta 9 img + 3 video + 3 audio),
  480p–1080p, 4–15s, 7 aspectos, audio nativo, seed.
- **Kling 3.0** (Pro/Standard): prompt único o **multi-toma**, imagen inicial y final, 3–15s, audio es/en, CFG, negativo.
- **OmniHuman** (1.5/1.0): avatar que habla desde imagen + audio (de tus voces generadas), turbo, 720p/1080p.
- **LipSync** (LatentSync): sincroniza labios de un video con un audio.
- Generación asíncrona con estado de cola; el MP4 baja solo al historial y a tu carpeta.

## Backup y portabilidad

- **Sincronización con iCloud** y **descarga .zip** del estudio (historial, proyectos, estilos, config; las claves no se incluyen).

## Claves (cada una local en su archivo)

- OpenAI → `~/.openai_key` (imagen, voz, transcripción) · ElevenLabs → `~/.elevenlabs_key` · fal.ai → `~/.fal_key` (video, música, upscale).

---

*App de un solo archivo, sin dependencias. Esta tanda se verificó con `jsc` (sintaxis JS) + una auditoría multi-agente.*
