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
| Texto atenuado | `--mut` | `#8c8c93` |
| Texto tenue | `--faint` | `#5d5d65` |
| Acento | `--accent` | `#e0a571` (ámbar) |
| Acento tenue | `--accent-dim` | `rgba(224,165,113,.14)` |
| OK | `--ok` | `#7bd99a` |
| Error | `--bad` | `#e57373` |

Nota de contraste: `--mut` (#8c8c93) sobre `--bg` ronda el límite para texto de cuerpo; reservarlo para labels/secundario, no para párrafos largos.

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
- **Modal de API key:** overlay con blur, icono en chip `--accent-dim`.
- **Ajustes avanzados:** `<details>` plegable.

## Layout

- Grid de 3 columnas (`362px / 1fr / 312px`) separadas por líneas finas de 1px; colapsa a 1 columna < 1180px.
- Controles a la izquierda, lienzo al centro, proyecto + historial a la derecha.
- Radios: 16px paneles/canvas, 10–11px controles, 7px chips, full-pill no usado en tarjetas.
- Sombras solo en modal/lightbox; el resto se separa con borde, no con sombra difusa.

## Motion

- Entrada escalonada de las 3 columnas (`rise`, ease-out, delays 0.02/0.09/0.16s).
- Spinner de carga; iconos flotantes con transición de opacidad/translate.
- Pendiente (mejora): alternativa `@media (prefers-reduced-motion: reduce)` para las animaciones de entrada.

## Known gaps / backlog

- Añadir bloque `prefers-reduced-motion`.
- Verificar contraste de `--mut` en textos secundarios largos.
- `Partial images` (preview en streaming) no implementado.
