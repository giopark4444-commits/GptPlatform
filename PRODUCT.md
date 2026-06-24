# Product

## Register

product

## Users

Gio, un creador que construye sus propias herramientas (vibecoding). Usa el estudio en su Mac, de forma local, para generar y editar imágenes con la API de OpenAI sin depender de la web de Platform ni de Claude. Contexto: trabajo individual, sesiones largas de iteración creativa, quiere control fino y ver el costo de cada generación. También está pensado para que cualquier usuario conecte su propia API key y lo use por su cuenta.

## Product Purpose

Un estudio local de generación de imágenes con `gpt-image-2` (100% gpt-image-2). Cubre todo el ciclo: crear desde texto, editar/combinar con imágenes de referencia, inpainting con máscara, control total de tamaño/calidad/formato/moderación, estimación de costo (aprox.) antes y costo por tokens después, historial persistente con reordenar/organizar/compartir, y "memoria de proyecto" (texto + referencias visuales) que da consistencia de estilo. Éxito = poder hacer en local, con buena UX, todo lo que la web de OpenAI Platform permite, más memoria de estilo por proyecto.

## Brand Personality

Preciso, premium, discreto. Tres palabras: nítido, profesional, enfocado. Debe sentirse como una herramienta de creativo (tipo Linear / Vercel / Arc): tranquila, densa donde importa, sin ruido. La interfaz no compite con la imagen generada; la enmarca.

## Anti-references

- Apps "baratas" con emojis como iconos, sombras difusas por todos lados y bordes súper redondeados.
- SaaS-cream genérico (fondos beige/arena, eyebrows en mayúsculas sobre cada sección, hero-metric).
- Dashboards saturados de tarjetas idénticas.
- Gradientes morados sobre blanco.

## Design Principles

- **La imagen manda.** La UI es marco neutro; el color y el contraste se reservan para el resultado, no para los controles.
- **Costo siempre visible.** El usuario nunca genera a ciegas: estimación (aprox.) antes, costo con tokens después, total de sesión al pie de la columna izquierda.
- **Densidad con respiro.** Muchos controles, pero agrupados y jerarquizados; lo avanzado se pliega.
- **Local y propio.** Todo vive en la Mac del usuario; la API key se conecta desde la app, sin intermediarios.
- **Paridad con Platform, y un paso más.** Igualar lo que ofrece OpenAI Platform y sumar memoria de estilo por proyecto.

## Accessibility & Inclusion

- Contraste de texto de cuerpo ≥ 4.5:1 sobre su fondo; etiquetas/valores legibles, no gris tenue ilegible.
- Respetar `prefers-reduced-motion` en las animaciones de entrada y micro-interacciones.
- Targets de clic cómodos en iconos flotantes y chips.
- Estados claros: vacío, cargando, error, resultado.
