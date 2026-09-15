# Egasis · Guía del piloto acompañado

Esta guía sirve para probar Egasis con una oferta concreta y decidir, con resultados observables, qué parte del trabajo comercial ayuda a resolver. El piloto puede empezar en simulación, sin enviar correos reales. Para pasar a envíos reales hacen falta las cuentas conectadas, una configuración revisada y el proceso de envío en funcionamiento.

## 1. Elegir un caso que podamos medir

Empezar con una sola oferta, un público definido y una campaña pequeña. Egasis no está limitado a quienes venden automatización con inteligencia artificial. Por ejemplo:

| Sector | Oferta de ejemplo | Público de ejemplo |
| --- | --- | --- |
| Arquitectura e interiores | Una conversación sobre la renovación de un local | Comercios que estén evaluando una reforma |
| Servicios contables | Una revisión inicial de necesidades administrativas | Empresas del tamaño y la zona que atiende el estudio |
| Proveedores de empresas | Presentar una línea de insumos y conocer requisitos | Responsables de compras del rubro correspondiente |
| Formación o consultoría | Explorar una necesidad de capacitación | Empresas que encajen con la especialidad del equipo |

Son ejemplos para adaptar. No implican que Egasis haya verificado una necesidad de compra ni que el contacto quiera recibir la propuesta.

Antes de configurar, escribir en pocas líneas: qué ofrecemos, a quién, qué información podemos demostrar, qué buscamos con el primer contacto y qué preguntas debe responder una persona del equipo. Evitar promesas de resultados, descuentos, precios o plazos que el negocio no haya aprobado.

## 2. Primera sesión: preparar y ensayar

La persona responsable del negocio recorre estos pasos con quien acompaña el piloto. Al terminar, debe poder repetir el recorrido por su cuenta.

1. **Crear el espacio.** Elegir “Crear espacio”, indicar el nombre del negocio, un correo y una contraseña de al menos 12 caracteres. Una instalación con registro restringido puede pedir una invitación. Completar oferta, público, firma y zona horaria del negocio.
2. **Probar en simulación.** Usar los datos de ejemplo o contactos de prueba. La simulación registra el recorrido sin entregar correos reales; respeta horarios y límites, por lo que una campaña fuera de horario no avanzará de inmediato. Quien acompaña confirma que el entorno sigue en simulación antes de procesar el ciclo de prueba.
3. **Crear una campaña.** Definir nombre, oferta, cliente objetivo, asunto y primer correo. Se pueden personalizar los textos con `{{name}}`, `{{company}}`, `{{offer}}`, `{{signature}}` y `{{booking_link}}`. Revisar los datos necesarios para que el mensaje tenga sentido. Egasis añade el enlace de baja.
4. **Elegir días, horario y límite diario.** Empezar con un volumen que el equipo pueda revisar y atender. La campaña se guarda como borrador. Al activarla, el primer correo se prepara y envía automáticamente dentro de esa configuración: no pide aprobación individual para cada destinatario. Los seguimientos están deshabilitados.
5. **Elegir cómo responder.** Usar una de las dos modalidades de la sección siguiente y comprobar el texto de la plantilla si corresponde.
6. **Agregar contactos de prueba y revisar el resultado.** Confirmar la campaña asignada, procesar un ciclo de simulación y abrir la conversación. Verificar destinatario, personalización, firma, enlace de baja y estado del mensaje. Un estado “simulado” no significa que se haya enviado un correo.

Para habilitar envíos reales, conectar una cuenta de correo compatible con SMTP e IMAP, usando las credenciales que permita su proveedor. Configurar el nombre del remitente, límite diario y espera entre mensajes. La configuración de las cuentas queda cifrada. Quien acompaña comprueba también el proceso de envío y la lectura de respuestas; tener abierta la página por sí solo no los pone en marcha.

## 3. Respuestas, dudas y aprendizajes

**Con revisión:** la persona lee la conversación, puede preparar un borrador con IA si está conectada, lo corrige y elige poner la respuesta en cola. El borrador no se envía por generarlo. Verificar especialmente nombres, hechos, precios, compromisos y la pregunta que queremos hacer.

**Automática con plantilla:** configurar previamente el texto de respuesta de la campaña. Cuando la clasificación con IA identifica interés o una consulta apta para ese recorrido, Egasis usa esa plantilla configurada. Las objeciones y los casos inciertos quedan para revisión. No asumir que esta modalidad resuelve cualquier pregunta ni que redacta y envía libremente una negociación. Si la IA no está disponible, revisar el caso sin contar con una respuesta automática.

Las bajas y exclusiones impiden nuevos envíos. Si aparece un envío incierto, comprobar la cuenta de correo antes de decidir qué ocurrió: no volver a enviarlo a ciegas. Una conversación mantiene la cuenta remitente que se le asignó.

**Aprendizajes del cliente:** guardar una nota breve sobre una preferencia, una aclaración de la oferta o una respuesta que conviene recordar. Se puede tomar como fuente un mensaje recibido de ese mismo espacio. La nota empieza como borrador y requiere aprobación de la persona responsable para entrar en el contexto de investigación y redacción con IA. Archivar una nota deja de incluirla en futuras generaciones; no reescribe los borradores anteriores.

Cada nota admite entre 10 y 2.000 caracteres y hay un máximo de 100 aprobadas. Conviene mantener pocas notas claras y actuales: el contexto tiene un límite y no incluye necesariamente todas a la vez. Las notas son referencias del negocio; no autorizan por sí mismas precios, garantías, reservas ni otros compromisos.

## 4. Contactos, investigación y materiales

**CSV:** importar un archivo o pegar una tabla con las columnas `email,name,company,website`. Se admiten hasta 1.000 contactos por importación y archivos de hasta 1 MB. Revisar los datos y su origen antes de importarlos. Egasis omite duplicados y conserva exclusiones previas; importar otra lista no vuelve a habilitar a alguien que se dio de baja.

**Apollo, opcional:** requiere una cuenta propia, integración configurada y la búsqueda expresamente habilitada. Buscar por los criterios disponibles, revisar la vista previa y elegir obtener e importar. Obtener datos puede consumir créditos de Apollo; se importan los correos verificados y utilizables que devuelva el proveedor. Que una búsqueda no encuentre resultados no se reemplaza con contactos inventados.

**Investigar un contacto:** cuando tiene sitio web, se puede guardar evidencia con su fuente y, si la IA está disponible, evaluar el encaje y preparar preguntas. Revisar esas fuentes antes de usar una conclusión. El encaje es una inferencia, no una certificación de la empresa ni una intención de compra. Si falta evidencia o la IA no está disponible, la ficha puede quedar sin evaluación completa.

Desde la ficha se pueden descargar materiales del contacto en texto y presentación. Revisarlos antes de compartirlos: tener una presentación generada no equivale a tener una propuesta comercial aprobada por el negocio.

## 5. Reuniones y rutina diaria

Se puede guardar una propuesta de reunión y confirmar o cancelar su estado. También se puede compartir el enlace de reserva del contacto para que elija una disponibilidad y confirme. Una propuesta o un enlace enviado todavía no cuentan como reunión confirmada por el prospecto.

Google Calendar es una conexión opcional. Con la conexión lista, Egasis puede consultar disponibilidad y gestionar el evento correspondiente. Un evento en el calendario del negocio no demuestra por sí solo que el prospecto haya aceptado ni implica que se haya enviado una invitación de calendario. También está disponible la descarga de la reunión en formato de calendario.

Al empezar cada jornada, revisar respuestas pendientes, objeciones, bajas, fallos y envíos inciertos. Corregir los borradores necesarios, comprobar reuniones y pausar campañas si el equipo no puede atender las conversaciones. Al cerrar, registrar resultados y minutos de trabajo manual.

## 6. Hoja de medición del piloto

Elegir una fecha de inicio y una de revisión. Registrar una muestra del trabajo habitual antes del piloto para comparar tareas equivalentes. No mezclar simulación con resultados de correo real.

| Qué registrar | Antes del piloto | Durante el piloto | Observaciones |
| --- | --- | --- | --- |
| Período, sector, oferta y campaña | | | |
| Contactos revisados e incorporados | | | |
| Primeros correos enviados / simulados, separados | | | |
| Respuestas recibidas y conversaciones útiles, revisadas por una persona | | | |
| Borradores útiles sin cambios / corregidos / descartados | | | |
| Motivos de corrección: hechos, tono, oferta o compromisos | | | |
| Reuniones propuestas / aceptadas por el prospecto / realizadas | | | |
| Bajas, fallos e inciertos pendientes | | | |
| Minutos para preparar contactos y primeros mensajes | | | |
| Minutos para revisar, responder y coordinar reuniones | | | |
| Minutos para corregir problemas y configurar el sistema | | | |
| Costos observados de los servicios conectados | | | |

Usar el panel como apoyo y contrastar los casos importantes con conversaciones y calendario. Un correo aceptado por el proveedor no garantiza llegada a la bandeja de entrada. El costo de IA mostrado es una estimación o reserva según la configuración, no la factura del proveedor.

En la revisión, responder: ¿qué tareas ahorraron tiempo?, ¿cuáles agregaron trabajo?, ¿qué respuestas fueron útiles?, ¿qué tuvo que corregir una persona? Decidir qué mantener, ajustar o pausar con esos registros. El piloto no promete una cantidad de ventas o reuniones ni una tasa de entrega.

## Descripción comercial reutilizable

Egasis ayuda a organizar el contacto comercial de un negocio: reúne contactos y campañas, envía un primer correo previamente configurado dentro de los horarios y límites elegidos, concentra conversaciones y permite revisar respuestas o usar una plantilla automática para los casos habilitados. Ofrece investigación y borradores con IA cuando está conectada, aprendizajes revisados por el negocio, materiales por contacto y coordinación de reuniones. Puede servir a profesionales, empresas de servicios, agencias y proveedores de distintos sectores.

Los precios de referencia actuales son Starter **USD 299/mes**, Growth **USD 799/mes** y Agency **USD 1.299/mes**, sujetos a lo aprendido durante el piloto. Estos nombres y precios no implican por ahora diferencias verificadas de volumen, acompañamiento o soporte. El piloto local puede utilizarse sin contratar una suscripción. Los servicios externos que se conecten tienen sus propias condiciones y costos.
