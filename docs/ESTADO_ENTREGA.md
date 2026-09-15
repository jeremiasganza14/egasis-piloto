# Egasis — estado de la entrega

Fecha de actualización: 15 de septiembre de 2026. Estado: **piloto publicado en simulación en [egasis-piloto.onrender.com](https://egasis-piloto.onrender.com)**. El objetivo completo sigue abierto: faltan el alta del propietario, el recorrido autenticado y la comprobación de persistencia después de reiniciar, además de las conexiones reales y el piloto con usuarios.

## Dirección respetada

La marca es Egasis. El producto admite profesionales, comercios, empresas, agencias y equipos con ofertas diversas; no exige vender automatización con IA. Cada negocio configura su oferta, audiencia, firma, cuentas y campañas. Activar una campaña prepara el primer correo configurado para envío automático, sin aprobación individual. Las respuestas posteriores siguen la política de la campaña: revisión manual o plantilla aprobada para interés/consultas clasificadas; las objeciones y los casos inciertos requieren intervención.

Las versiones anteriores, exports y bases originales se conservaron. El desarrollo nuevo está en `Egasis_Workspace/egasis`. Solo se pobló un espacio local de demostración con datos ficticios y se actualizó esa base nueva a esquema v3. No se migraron datos reales, no se enviaron correos reales, no se hicieron cobros ni se compró alojamiento.

## Evidencia obtenida

- `python3 -m pytest -q`: 306 pruebas aprobadas, 12 pruebas PostgreSQL omitidas por necesitar su entorno específico y 15 subpruebas aprobadas. Ejecutado el 15 de septiembre después de integrar API, interfaz, aprendizajes, migraciones, respaldo y configuración Render Free.
- Las 12 pruebas PostgreSQL se ejecutaron además contra PostgreSQL 17.11 real y aprobaron en 5,04 segundos. Incluyen v1/v2/v3, concurrencia, separación de clientes, presupuesto y respaldo/restauración. El clúster temporal fue apagado. [Evidencia PostgreSQL](POSTGRES_QA.md).
- Navegador: inicio de sesión de demostración, edición y activación de campaña, primer correo simulado con contador real en cero, respuesta manual puesta en cola y luego simulada, cola vacía al finalizar, creación/aprobación de aprendizaje, elección de horario y confirmación explícita de reserva. Inspección visual a 1280 px y en ventana angosta de 639 px. No se afirma una revisión exhaustiva de todos los dispositivos o tecnologías de asistencia.
- PowerPoint: dos presentaciones sintéticas de cinco diapositivas; diez imágenes revisadas, más validación estructural, geométrica e importación. No se verificó con PowerPoint nativo. [Evidencia de materiales](MATERIALS_QA.md).
- Copia consistente de la base ficticia v2 antes de migrarla a v3; las pruebas de recuperación usan archivos temporales. La clave se conserva separada y nunca entra al paquete fuente.
- Publicación en Render Free el 15 de septiembre: despliegue `dep-dakrvvbl550s73alf650` con estado `Deploy succeeded` / `Live`, desde el commit `92fb10bb614d22e7303b739c687cbeabf7b00a21`; construcción de 1 minuto y 10 segundos. Los registros muestran `configuration_ready: true`, PostgreSQL, simulación activa, registro por invitación y arranque completo. `GET /api/health` y `GET /` respondieron HTTP 200. Esto acredita publicación y arranque; todavía no acredita el recorrido autenticado ni la conservación de cambios después de reiniciar.

## Auditoría contra la propuesta aprobada

| Etapa original | Evidencia actual | Estado respecto del alcance completo |
| --- | --- | --- |
| 1. Base reproducible | Dependencias fijadas, aplicación y worker separados, Dockerfile, esquema versionado, [mapa API](API.md), fixtures de fallos; arranque local y construcción/arranque en Render Free comprobados. | Publicación del piloto verificada. Pendientes recorrido autenticado y prueba de persistencia tras reinicio en este alojamiento. |
| 2. Operación confiable | Correo con cuenta fija, deduplicación por identificadores, cola persistente, reservas de cuota, pausa/baja, incertidumbre sin reintento ciego, IMAP completo antes de enviar y métricas de resultados/costos. Tests de mail, worker, API, IA y PostgreSQL. | Verificada con proveedores simulados y base real local. La autenticación y entregabilidad del correo real siguen pendientes. |
| 3. Producto por cliente | Espacios, propietarios/permisos, ofertas, contactos, exportaciones, conexiones cifradas, trabajos y suscripciones con identidad de espacio. Tests de acceso cruzado y aislamiento PostgreSQL. | Base implementada. Alta autoservicio de equipos, recuperación de contraseña por email y onboarding OAuth de un clic no forman parte de esta interfaz inicial acompañada. |
| 4. Autonomía verificable | Investigación con evidencia, reserva de presupuesto, borradores, plantilla automática configurada, aprendizajes aprobados/archivados, recuperación de trabajos y reservas confirmadas. | Implementación y regresiones verificadas. Falta evaluación con el modelo/cuentas reales y ofertas concretas del piloto; no se promete ausencia universal de errores de IA. |
| 5. Piloto controlado | Circuito de demostración y [guía con hoja de medición](GUIA_DEL_PILOTO.md). | Pendiente el grupo real de clientes, sus conexiones autorizadas y la medición de utilidad, costo e intervención. Las métricas ficticias no son resultados comerciales. |
| 6. Comercialización | Stripe opcional con firma e idempotencia probadas, política de estado de suscripción, guía repetible, descripción comercial y precios de referencia. | Pendientes cuenta/precios reales de Stripe, validación externa y alcance comercial de cada plan. Los tres planes no tienen cuotas diferenciadas implementadas. |

## Límites de la primera instalación

La IA no es necesaria para el primer correo configurado ni para responder manualmente. Investigación y borradores sí requieren Gemini. La respuesta automática usa la plantilla del propietario; no negocia libremente precios o compromisos. No hay seguimientos automáticos a quien no respondió.

Apollo es un conector opcional con credenciales propias y habilitación administrativa; la autorización del proveedor y el consumo de créditos deben comprobarse antes de usarlo. La importación CSV funciona sin Apollo. Google Calendar requiere configuración OAuth acompañada; los enlaces privados también permiten reservar dentro de Egasis sin Google. Las reservas no envían invitaciones de calendario a terceros.

La interfaz permite verificar envíos inciertos contra evidencia del proveedor. Una restauración retiene trabajos y contactos para revisión y revoca sesiones: no reactiva campañas por sí sola. [Recuperación](RECOVERY.md).

## Publicación gratuita ejecutada

[GRATIS.md](GRATIS.md) documenta la instalación publicada: Render Free para web/API y Neon Free para PostgreSQL, en simulación, con un único servicio web de Egasis y sin worker permanente. La dirección es [https://egasis-piloto.onrender.com](https://egasis-piloto.onrender.com). [HOSTING.md](HOSTING.md) conserva alternativas futuras de pago.

El 15 de septiembre se verificaron las sesiones de GitHub, Render y Neon. Se creó el repositorio privado `jeremiasganza14/egasis-piloto` y una base nueva Neon Free, `Egasis piloto`, con PostgreSQL 17 en Ohio. Los proyectos y servicios anteriores se conservaron. Antes de crear el servicio, Render mostró plan Hobby, sin tarjeta, gasto actual/proyectado de USD 0 y cuotas disponibles. El usuario autorizó guardar la conexión privada de Neon en Render y se completó esa configuración. Las claves no se incluyen en el código, esta guía ni el chat.

El servicio `srv-dakrvurl550s73alf3m0` pertenece al Blueprint `exs-daklqugu01pc73fjb1l0`. Se verificó en el panel **Auto Sync: No** y **Sync paused**; los despliegues automáticos del servicio también están desactivados en la plantilla. La evidencia del despliegue figura arriba.

Quedan pendientes crear el propietario con la invitación privada, ingresar y recorrer una campaña simulada, y comprobar que los cambios continúen en Neon después de reiniciar Render. No se afirma que exista ya un usuario autenticado en la instalación pública. Luego siguen las conexiones autorizadas y la evaluación con clientes reales.

La preferencia vigente del usuario es **gratis por ahora, sin tarjeta ni upgrades**. No se contrató ningún servicio pago; Railway Pro dejó de ser la opción a seguir. El alojamiento gratuito mantiene límites compartidos con los servicios anteriores. Los proveedores externos y un eventual worker local se evaluarían por separado, sin prometer campañas reales gratuitas las 24 horas.
