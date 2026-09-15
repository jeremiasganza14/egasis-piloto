# Verificación de materiales de reunión

Fecha de cierre: 9 de septiembre de 2026.

## Alcance

Se verificaron `build_brief`, `export_markdown` y `export_pptx` de `egasis/materials.py`. Las pruebas utilizaron bases temporales y datos sintéticos. No se consultaron bases reales, credenciales, servicios de IA, sitios de contactos ni servicios de correo.

El exportador construye una presentación editable de cinco diapositivas: contexto, oferta, conversación, preguntas y próximo paso. La plantilla se creó con `@oai/artifact-tool` del runtime de presentaciones. El código de producción sustituye texto dentro de esa plantilla con la biblioteca estándar de Python. No requiere Node, python-pptx ni un servicio externo en producción.

## Defectos encontrados y corregidos

1. El recorte original por cantidad de caracteres permitía desbordes con palabras largas de letras anchas. Una empresa de 200 letras `W` invadía el bloque del contacto. Una oferta de 5.000 letras `W` invadía la sección del público. Se reemplazó ese recorte por ajuste conservador de ancho y cantidad de líneas. El texto visible termina con puntos suspensivos cuando corresponde y el contenido capturado por la ficha permanece en las notas.
2. JSON puede transportar sustitutos Unicode aislados y caracteres que XML 1.0 no admite. Ahora el exportador elimina esos caracteres antes de escribir los nodos del PPTX, evitando archivos XML inválidos.

## Pruebas automatizadas

Ejecutado desde la carpeta del proyecto:

```text
python3 -m pytest tests/test_materials.py -q
18 passed
```

La suite cubre:

- Ofertas de un estudio contable y un proveedor de equipamiento industrial, sin contenido de un nicho fijo.
- Prioridad de la oferta de campaña sobre la configuración del espacio.
- Separación de contactos, campañas, mensajes y reuniones entre espacios.
- Exclusión de borradores sin aprobar y de sus afirmaciones de precio o cierre comercial.
- Distinción entre mensajes enviados y salidas simuladas.
- Estados de reunión obtenidos de registros. Un correo o enlace de reserva nunca crea una reunión confirmada.
- Reuniones propuestas, confirmadas, canceladas y pasadas, y contactos excluidos de nuevos mensajes.
- Ausencia explícita de oferta o investigación atribuida.
- Texto Markdown que no activa HTML ni imágenes remotas procedentes de datos de origen.
- Cinco diapositivas editables con notas, sin marcadores de plantilla pendientes ni recursos externos.
- Conservación del contenido capturado en notas al acortar diapositivas.
- Entradas anchas sin espacios y caracteres XML inválidos.

## Render e inspección visual

Se generaron dos archivos de muestra, ambos con cinco diapositivas. Cada uno pasó la validación estructural, de geometría y de importación del runtime. Los informes registraron cero hallazgos de estructura, cero hallazgos de disposición y cero advertencias de disposición.

| Muestra | Contenido | Inspección |
| --- | --- | --- |
| `sample-v2` | Proveedor industrial ficticio, contacto ficticio, conversación y reunión propuesta | Las cinco imágenes se revisaron individualmente. Texto legible, márgenes consistentes y separación correcta entre bloques. El horario aparece pendiente de confirmar. |
| `stress-v2` | Empresa de 200 letras anchas, nombre de 160, oferta de 5.000, público de 2.000 y mensaje de 1.000 sin espacios | Las cinco imágenes se revisaron individualmente. El recorte conserva los límites de cada bloque y elimina las superposiciones observadas antes de la corrección. |

Los artefactos privados de esta comprobación están en `/tmp/egasis-materials-build/`:

- Presentaciones: `output/sample-v2-final.pptx` y `output/stress-v2-final.pptx`.
- Informes: `.build/sample-v2-validation.json` y `.build/stress-v2-validation.json`.
- Imágenes: `.build/sample-v2-1.png` a `.build/sample-v2-5.png`, y `.build/stress-v2-1.png` a `.build/stress-v2-5.png`.
- Los archivos de `/tmp` son evidencia temporal y no forman parte de la entrega del producto.

## Límites de la comprobación

La inspección visual se hizo sobre PNG de 1280 × 720 generados por Artifact Tool. No se abrió PowerPoint de escritorio ni Google Slides, por lo que no se afirma validación nativa en esas aplicaciones. La sustitución de la fuente Helvetica Neue en otro sistema puede alterar ligeramente el texto.

Las diapositivas son extractos para preparar una conversación. La ficha captura hasta 50 mensajes, hasta 8.000 caracteres por mensaje y hasta 16.000 de evidencia web. El historial completo continúa en Egasis. El texto del sitio y los mensajes se atribuyen a sus fuentes y no constituyen verificación independiente. Las preguntas son sugerencias y el exportador no deduce precios, métricas, acuerdos ni reservas.
