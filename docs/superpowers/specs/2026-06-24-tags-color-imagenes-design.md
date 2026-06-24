# Tags de color (multi-color) en imágenes — Gio Studio

Fecha: 2026-06-24

## Objetivo

Permitir marcar cada imagen —tanto en **Historial** (generadas) como en
**Mis imágenes**— con **varios de 4 colores fijos** a la vez, mostrarlos en la
tarjeta, y **filtrar** por color (junto al filtro ★ existente).

## Colores

Paleta fija de 4, sin nombre (estilo etiquetas de Finder):

| clave  | color   | hex      |
|--------|---------|----------|
| `r`    | rojo    | `#e5484d`|
| `y`    | amarillo| `#f5b400`|
| `g`    | verde   | `#46a758`|
| `b`    | azul    | `#3b82f6`|

## Modelo de datos

Cada imagen ya es un dict en su `_info.json`:
- Historial → `phist_json(proj, sub)`
- Mis imágenes → `pshelf_json(proj, sub)`

Se añade un campo opcional `colors: []` (lista de claves de color activas, p.ej.
`["r","b"]`). Ausente/`[]` = sin color. Mismo patrón que el `fav` existente en
Historial.

## Backend

Endpoint nuevo `POST /imgcolors`:
```json
{ "file": "...", "colors": ["r","b"], "project": "...", "sub": "...", "scope": "hist" | "shelf" }
```
- `scope=hist` → parchea `phist_json`; `scope=shelf` → parchea `pshelf_json`.
- Saneo: solo se aceptan claves de la paleta (`r,y,g,b`), deduplicadas.
- Registrado en la tabla de rutas POST junto a `/histfav`.
- Handler espejo de `h_histfav` (mismo LOCK, `os.path.basename`, load/save_json).

## Frontend — 3 superficies

Todas ya existen; se les suma lo mismo:

1. **Historial en la app** — `gcardHtml` (~3064) + `renderGal` (~3074) + el
   handler de clics del `#gal` (~3278). El toggle ★ usa `/histfav`; el de colores
   usa `/imgcolors` con `scope:"hist"`.
2. **Mis imágenes en la app** — `scardHtml` (~4539) + `renderShelf` + su handler
   de clics. Usa `/imgcolors` con `scope:"shelf"`.
3. **Ventana "Ver todo"** — `gallery_html` (~4999), su HTML de tile y su JS
   inline (~5168). Misma marca + filtro; `scope` según `SRC`.

### Interacción
- Al hover, junto al ★ aparece un selector de 4 puntos de color. Clic = toggle
  de ese color (multi-selección).
- Los colores activos se muestran como puntitos en la esquina de la tarjeta
  **siempre** (no solo en hover).

### Filtro
- Junto al botón ★ (`#galFavBtn` en Historial; equivalente en shelf y en la
  ventana) se añaden 4 puntitos de color clicables que actúan como toggles de
  filtro.
- Semántica **OR**: si hay ≥1 color de filtro activo, se muestran las imágenes
  que tengan **cualquiera** de esos colores. El filtro ★ y el de color se
  combinan con AND entre sí (como hoy ★ se combina con la búsqueda de texto).

## Fuera de alcance (YAGNI)

- Nombres/edición de colores.
- Filtro AND entre colores.
- Sincronizar colores al mover una imagen entre Historial y Mis imágenes
  (se conservan en su JSON; mover no es parte de esta feature).

## Verificación

- Marcar/desmarcar colores en Historial y en Mis imágenes; recargar y comprobar
  persistencia en el `_info.json` correspondiente.
- Filtrar por uno y varios colores en las 3 superficies.
- Imágenes sin `colors` siguen funcionando (retrocompatibilidad).
