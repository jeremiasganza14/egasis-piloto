# Egasis — estado de la entrega

Fecha de actualización: 15 de septiembre de 2026. Estado: aplicación local verificada y preparada para instalar un piloto. **El objetivo completo sigue abierto: falta publicar, conectar proveedores reales y completar el piloto con usuarios.**

## Dirección respetada

La marca es Egasis. El producto admite profesionales, comercios, empresas, agencias y equipos con ofertas diversas; no exige vender automatización con IA. Cada negocio configura su oferta, audiencia, firma, cuentas y campañas. Activar una campaña prepara el primer correo configurado para envío automático, sin aprobación individual. Las respuestas posteriores siguen la política de la campaña: revisión manual o plantilla aprobada para interés/consultas clasificadas; las objeciones y los casos inciertos requieren intervención.

Las versiones anteriores, exports y bases originales se conservaron. El desarrollo nuevo está en `Egasis_Workspace/egasis`. Solo se pobló un espacio local de demostración con datos ficticios y se actualizó esa base nueva a esquema v3. No se migraron datos reales, no se enviaron correos reales, no se hicieron cobros ni se compró alojamiento.

## Evidencia obtenida

- `python3 -m pytest -q`: 306 pruebas aprobadas, 12 pruebas PostgreSQL omitidas por necesitar su entorno específico y 15 subpruebas aprobadas. Ejecutado el 15 de septiembre después de integrar API, interfaz, aprendizajes, migraciones, respaldo y configuración Render Free.
- Las 12 pruebas PostgreSQL se ejecutaron además contra PostgreSQL 17.11 real y aprobaron en 5,04 segundos. Incluyen v1/v2/v3, concurrencia, separación de clientes, presupuesto y respaldo/restauración. El clúster temporal fue apagado. [Evidencia PostgreSQL](POSTGRES_QA.md).
- Navegador: inicio de sesión de demostración, edición y activación de campaña, primer correo simulado con contador real en cero, respuesta manual puesta en cola y luego simulada, cola vacía al finalizar, creación/aprobación de aprendizaje, elección de horario y confirmación explícita de reserva. Inspección visual a 1280 px y en ventana angosta de 639 px. No se afirma una revisión exhaustiva de todos los dispositivos o tecnologías de asistencia.
- PowerPoint: dos presentaciones sintéticas de cinco diapositivas; diez imágenes revisadas, más validación estructural, geométrica e importación. No se verificó con PowerPoint nativo. [Evidencia de materiales](MATERIALS_QA.md).
- Copia consistente de la base ficticia v2 antes de migrarla a v3; las pruebas de recuperación usan archivos temporales. La clave se conserva separada y nunca entra al paquete fuente.

## Auditoría contra la propuesta aprobada

| Etapa original | Evidencia actual | Estado respecto del alcance completo |
| --- | --- | --- |
| 1. Base reproducible | Dependencias fijadas, aplicación y worker separados, Dockerfile, esquema versionado, [mapa API](API.md), fixtures de fallos; arranque local comprobado. | Implementación local verificada. Build y arranque en el alojamiento final todavía pendientes. |
| 2. Operación confiable | Correo con cuenta fija, deduplicación por identificadores, cola persistente, reservas de cuota, pausa/baja, incertidumbre sin reintento ciego, IMAP completo antes de enviar y métricas de resultados/costos. Tests de mail, worker, API, IA y PostgreSQL. | Verificada con proveedores simulados y base real local. La autenticación y entregabilidad del correo real siguen pendientes. |
| 3. Producto por cliente | Espacios, propietarios/permisos, ofertas, contactos, exportaciones, conexiones cifradas, trabajos y suscripciones con identidad de espacio. Tests de acceso cruzado y aislamiento PostgreSQL. | Base implementada. Alta autoservicio de equipos, recuperación de contraseña por email y onboarding OAuth de un clic no forman parte de esta interfaz inicial acompañada. |
| 4. Autonomía verificable | Investigación con evidencia, reserva de presupuesto, borradores, plantilla automática configurada, aprendizajes aprobados/archivados, recuperación de trabajos y reservas confirmadas. | Implementación y regresiones verificadas. Falta evaluación con el modelo/cuentas reales y ofertas concretas del piloto; no se promete ausencia universal de errores de IA. |
| 5. Piloto controlado | Circuito de demostración y [guía con hoja de medición](GUIA_DEL_PILOTO.md). | Pendiente el grupo real de clientes, sus conexiones autorizadas y la medición de utilidad, costo e intervención. Las métricas ficticias no son resultados comerciales. |
| 6. Comercialización | Stripe opcional con firma e idempotencia probadas, política de estado de suscripción, guía repetible, descripción comercial y precios de referencia. | Pendientes cuenta/precios reales de Stripe, validación externa y alcance comercial de cada plan. Los tres planes no tienen cuotas diferenciadas implementadas. |

## Límites de la primera instalación

La IA no es necesaria para el primer correo configurado ni para responder manualmente. Investigación y borradores sí requieren Gemini. La respuesta automática usa la plantilla del propietario; no negocia libremente precios o compromisos. No hay seguimientos automáticos a quien no respondió.

Apollo es un conector opcional con credenciales propias y habilitación administrativa; la autorización del proveedor y el consumo de créditos deben comprobarse antes de usarlo. La importación CSV funciona sin Apollo. Google Calendar requiere configuración OAuth acompañada; los enlaces privados también permiten reservar dentro de Egasis sin Google. Las reservas no envían invitaciones de calendario a terceros.

La interfaz permite verificar envíos inciertos contra evidencia del proveedor. Una restauración retiene trabajos y contactos para revisión y revoca sesiones: no reactiva campañas por sí sola. [Recuperación](RECOVERY.md).

## Publicación preparada, aún no ejecutada

[GRATIS.md](GRATIS.md) deja la opción elegida para preparar el piloto: Render Free para web/API y Neon Free para PostgreSQL, en simulación, con un único servicio web y sin worker permanente. [HOSTING.md](HOSTING.md) conserva alternativas futuras de pago. El dominio inicial puede ser el generado por el proveedor.

El 15 de septiembre se verificaron las sesiones de GitHub, Render y Neon. Se creó el repositorio privado `jeremiasganza14/egasis-piloto` y una base nueva Neon Free, `Egasis piloto`, con PostgreSQL 17 en Ohio. Los proyectos anteriores se conservaron. Render muestra plan Hobby, sin tarjeta, gasto actual/proyectado de USD 0 y cuotas disponibles. El nuevo servicio web todavía no se creó: queda pendiente autorizar el almacenamiento de la conexión privada de Neon en Render, desplegar y validar el recorrido público. Las claves no se incluyen en el código, esta guía ni el chat.

La preferencia vigente del usuario es **gratis por ahora, sin tarjeta ni upgrades**. No se autorizó un pago y la propuesta anterior de Railway Pro dejó de ser la opción a seguir. Está preparada la configuración gratuita; no se contrató ningún servicio pago y la web aún no se publicó. Los servicios externos y un eventual worker local se evaluarían por separado, sin prometer campañas reales gratuitas las 24 horas.
