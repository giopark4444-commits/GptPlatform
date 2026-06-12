# Design

Sistema visual del Estudio (app local, una sola página servida por `server.py`). Tema oscuro, minimalista, premium. Registro: product (la UI sirve a la tarea, no es el producto).

## Theme

Dark, refinado y utilitario (referencia mental: Linear / Vercel / Arc). Fondo casi negro con un brillo ámbar muy sutil arriba a la derecha. El acento ámbar se usa con moderación: estados activos, valores numéricos y el costo. La acción primaria es blanca (alto contraste sobre el negro).

Estrategia de color: **Restrained** (neutros tintados + un acento ≤10%).

## Color Palette

Tokens actuales (CSS variables en `:root`):

| Rol | Token | Valor |
|---|---|---|
| Fondo | `--bg` | `#0a0a0b` |
| Superficie | `--surface` | `#101012` |
| Superficie 2 | `--surface2` | `#161618` |
| Elevado | `--elev` | `#1c1c1f` |
| Línea | `--line` | `rgba(255,255,255,.06)` |
| Línea 2 | `--line2` | `rgba(255,255,255,.11)` |
| Texto | `--txt` | `#ededee` |
| Texto atenuado | `--mut` | `#9a9aa1` |
| Texto tenue | `--faint` | `#67676f` |
| Acento | `--accent` | `#e0a571` (ámbar) |
| Acento tenue | `--accent-dim` | `rgba(224,165,113,.14)` |
| OK | `--ok` | `#7bd99a` |
| Error | `--bad` | `#e57373` |

Nota de contraste: `--mut` y `--faint` se subieron un paso (2026-06) para cumplir AA en texto secundario. Z-index semántico en tokens: `--z-sticky` < `--z-modal` < `--z-lightbox` < `--z-toast`.

## Typography

Dos familias por eje de contraste (grotesque + mono), sin fuentes genéricas:

- **UI / display:** `Schibsted Grotesk` (400/500/600/700).
- **Mono (valores, costos, tamaños, ratios):** `Geist Mono` (400/500).
- Cargadas vía Google Fonts.

Reglas: micro-labels en mayúsculas, 10px, `letter-spacing:.11em`, color `--faint`. Valores numéricos siempre en mono con `tabular-nums`. Jerarquía por peso + tamaño, no por color.

## Components

- **Top bar:** marca con mark ámbar, segmented control Crear/Editar con iconos, costo de sesión (mono), botón fantasma "API".
- **Segmented control:** pill en `--surface2`, activo en `--elev`.
- **Sliders custom:** track fino `--line2`, thumb blanco que pasa a ámbar en hover.
- **Chips de preset:** mono, con miniatura de proporción (rectángulo a escala real del aspect ratio); activo en `--accent-dim`.
- **Dropzones:** borde punteado, hover ámbar.
- **Canvas 4:3:** fondo de cuadrícula sutil; imagen `object-fit:contain`; iconos flotantes (copiar prompt+refs, usar como referencia, descargar) que aparecen en hover; clic abre lightbox a pantalla completa.
- **Galería de historial:** miniaturas 1:1 con los mismos iconos flotantes en hover; caption con costo y tamaño en mono.
- **Botón primario:** blanco, texto oscuro, micro-elevación en hover.
- **Modal de API key:** overlay con blur, icono en chip `--accent-dim`. Botón API con punto de estado (`--ok` cuando hay clave).
- **Ajustes avanzados:** `<details>` plegable, incluye carpeta de guardado configurable.
- **Toasts:** pill superior centrada en `--elev`, punto de estado ok/bad, animación de entrada sutil. Sustituyen a `alert()`.
- **Editor de imagen:** modal grande con pestañas (segmented control) Máscara · Anotar · Pins. Máscara: trazo ámbar al 55% (pincel/borrador/rect/lazo). Anotar: capa opaca en rojo `#e5483f` (flecha/círculo/trazo/texto, deshacer). Pins: badges rojos numerados + lista lateral de instrucciones. El rojo solo existe como tinta de anotación sobre la imagen, nunca en el chrome de la UI.
- **Tira de resultados:** miniaturas 62px bajo el lienzo cuando hay >1 imagen; activa con anillo ámbar.
- **Lightbox:** barra inferior flotante con prompt truncado, "usar prompt" y descargar; `Esc` cierra.
- **Historial:** filtro por proyecto, contador, "ver más", papelera con confirmación armada (doble clic, estado rojo); tarjetas arrastrables a referencias.
- **kbd:** chips mono 10px con borde inferior doble para los atajos (`⌘↵`, `1`, `2`, `3`, `⌘V`).
- **Video:** cuarto modo del segmented principal. Select de modelo (Seedance/Seedance Fast/Kling Pro/OmniHuman) que muestra u oculta los controles propios de cada uno; tarjeta de progreso con spinner + estado de cola en vivo; resultado en `.audcard` con `<video>`; historial en filas `.arow` con icono de reproducción que carga el video en el centro. Estimador en $ por segundo según modelo/resolución.
- **Audio:** tercer modo del segmented principal. Sub-tabs Voz/Transcribir/Efectos (mismo seg) y dentro de Voz otro seg OpenAI/ElevenLabs. Voces OpenAI como chips mono; voces ElevenLabs en select con optgroups por categoría (Clonadas/Generadas/Profesionales/Predefinidas). Estilos de voz guardados como chips con × al estilo del sistema y chip "+ Guardar actual" punteado en ámbar. Tarjetas `.audcard` (radio 16px) para player/transcripción, filas `.arow` compactas en el historial de audio con botón play que pasa a ámbar al reproducir; costo en $ (OpenAI) o créditos (ElevenLabs). Controles que no aplican al modelo elegido se atenúan con `.dim`, no se ocultan.

## Layout

- Grid de 3 columnas (`362px / 1fr / 312px`) separadas por líneas finas de 1px; colapsa a 1 columna < 1180px.
- Controles a la izquierda, lienzo al centro, proyecto + historial a la derecha.
- Radios: 16px paneles/canvas, 10–11px controles, 7px chips, full-pill no usado en tarjetas.
- Sombras solo en modal/lightbox; el resto se separa con borde, no con sombra difusa.

## Motion

- Entrada escalonada de las 3 columnas (`rise`, ease-out, delays 0.02/0.09/0.16s).
- Spinner de carga; iconos flotantes con transición de opacidad/translate; toasts con entrada de 250ms.
- `@media (prefers-reduced-motion: reduce)`: sin entrada escalonada, sin animación de toasts, transiciones a ~0; el spinner se conserva más lento (conviene como señal de carga).

## Accesibilidad

- `:focus-visible` con anillo ámbar en todos los controles (los campos conservan su foco por borde).
- Contraste AA en texto secundario (`--mut` #9a9aa1 sobre `--bg`).
- `alt`/`title` en miniaturas; `loading="lazy"` en el historial.

## Known gaps / backlog

- `Partial images` (preview en streaming) no implementado.
