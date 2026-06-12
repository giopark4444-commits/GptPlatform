# GptPlatform

Estudio local de generación y edición de imágenes con la API de OpenAI
(`gpt-image-2`, y `gpt-image-1` para fondo transparente). Una sola página servida
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
- **Atajos**: `⌘↵` genera, `1`/`2` cambia Crear/Editar, `Esc` cierra modales y lightbox.
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
