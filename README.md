# GptPlatform — **Gio Studio**, estudio local de imagen, audio y video con OpenAI

Estudio web **local** (apodo de la app: **Gio Studio**) para crear y editar **imágenes** (OpenAI `gpt-image-2`),
**voz/transcripción/música** y **video** — todo desde una sola página servida por un script de Python
**sin dependencias externas** (solo librería estándar). La idea: hacer en tu propio Mac, con buena UX, lo que
permite OpenAI Platform (+ proveedores extra vía fal.ai y ElevenLabs), y que **nada se quede en la nube salvo lo que tú generas**.

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
- **Costos (aprox.)**: estimador antes de generar (calibrado a la tabla oficial, incl. el ajuste de no-cuadrados y que
  `auto` factura como `medium`/`high`), costo por tokens después (texto $5 / imagen de entrada $8 / cacheada $2 por 1M, separados),
  total de sesión (al pie de la columna izquierda) y desglose salida/entrada. Todos los precios se muestran como **"aprox."** porque la
  factura final puede variar. **Estimado del lote** con confirmación antes de lanzar.
- **Mejorar prompt con IA** (✨, gpt-4o-mini) · **Describir** (imagen → prompt, visión con `detail`) en historial y estante.
- **🗂️ Mis imágenes (estante local)**: carga/arrastra imágenes propias que se **guardan en tu equipo** (no en OpenAI)
  y quedan siempre a la mano debajo del lienzo; **carpeta configurable**, y por imagen: usar como referencia, describir, descargar, **compartir**, quitar.
- **Carpeta de guardado** con **selector nativo de macOS** (botón "Examinar"), o ruta a mano.
- **Historial pro**: una imagen por fila, búsqueda por prompt, favoritas ★, filtro por proyecto/subproyecto,
  **Comparador A/B** a pantalla completa con slider, **Iterar** sobre un resultado, borrar, y arrastrar a referencias.
  **Menú "Organizar"** (por fecha de creación recientes/antiguas, o por nombre — así siempre vuelves al orden de creación).
- **Reordenar arrastrando**: en Historial y Mis imágenes (y en sus ventanas "Ver todo") arrastras una imagen a su nueva
  posición y las demás **abren espacio con animación fluida**; el orden se guarda.
- **Selección múltiple**: botón *Seleccionar* + **clic**, **arrastrar un recuadro** (marquee) o **Shift-clic de rango**;
  luego acciones en lote (borrar, a Mis imágenes) o **arrastrar la selección entera** a Referencias o a otro subproyecto.
- **Visor flotante**: al hacer clic en una imagen se amplía con **Pantalla completa**, **resolución real** del archivo,
  **prompt seleccionable/copiable** y las **imágenes de referencia que se usaron** debajo del prompt.
- **Compartir por imagen**: botón de compartir → hoja del sistema, **WhatsApp · Telegram · Instagram · Facebook · X**, copiar imagen o descargar.
- **Aviso "Generando" flotante** (arriba, te sigue al hacer scroll) con **cronómetro**; timeout de seguridad para que nunca se quede pegado.
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

## Backup, importar y portabilidad

- **Sincronización con iCloud**.
- **Respaldo .zip organizado** (carpetas legibles: Historial / Mis imágenes / Subproyectos) con **prompts y las imágenes de referencia** — para revisar a mano.
- **Copia exacta** (clon completo de `~/image-studio`) que con **Importar** restaura todo **tal cual** (imágenes, prompts, referencias, estante, proyectos/subproyectos, estilos y config).
- Las descargas muestran **barra de progreso en dos pasos** (1/2 Preparando · 2/2 Descargando) y dejan **elegir dónde guardar** el archivo. La importación valida la ruta (anti path-traversal) y restaura por streaming.
- Las **claves API nunca se incluyen** (se conectan una vez por equipo).

## Claves (cada una local en su archivo)

- OpenAI → `~/.openai_key` (imagen, voz, transcripción) · ElevenLabs → `~/.elevenlabs_key` · fal.ai → `~/.fal_key` (video, música).

---

*App de un solo archivo, sin dependencias. Las tandas recientes se verificaron con `python -m py_compile` + `node --check`
(sintaxis) y **pruebas en vivo en el navegador** (Chrome DevTools): reorder, selección/arrastre, compartir, visor,
backup/importar y recuperación de generación.*
