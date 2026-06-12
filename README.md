# GptPlatform

Estudio local de generación y edición de imágenes con la API de OpenAI
(`gpt-image-2`, y `gpt-image-1` para fondo transparente). Una sola página servida
por un script de Python sin dependencias externas. La idea: hacer en local, con
buena UX, todo lo que permite OpenAI Platform, y sumar **memoria de estilo por proyecto**.

![tema oscuro · minimalista](#)

## Qué hace

- **Crear** (texto → imagen) y **Editar / combinar** (sube imágenes de referencia + prompt).
- **Inpainting** con máscara.
- **Tamaño libre** con sliders + presets completos (social, foto, **cine / anamórfico**, alta resolución), candado de proporción y miniaturas de cada aspect ratio.
- **Calidad, formato (PNG/JPG/WebP), compresión, moderación, cantidad**.
- **Fondo transparente** (cambia automáticamente a `gpt-image-1`).
- **Estimador de costo** antes de generar + **costo real** (tokens) después + total de sesión.
- **Historial persistente** (galería 1:1 con descargar / copiar prompt / usar como referencia).
- **Memoria de proyecto**: `estilo.md` (texto que se antepone) + **referencias visuales** que se adjuntan solas al generar. Botón para **destilar el estilo** con IA.
- **Lightbox** a pantalla completa e **iconos flotantes** sobre cada imagen.
- Pantalla para **conectar tu API key** desde el navegador (se guarda en `~/.openai_key`).

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

- La clave vive solo en tu equipo (`~/.openai_key`), nunca en el repo.
- Las imágenes generadas se guardan en `~/image-studio/historial/` y también en el Escritorio.
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
