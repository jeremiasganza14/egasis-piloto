# Egasis

Plataforma web de prospección para negocios, profesionales, agencias y equipos que venden productos o servicios. Cada espacio configura su oferta, público, firma, campañas y cuentas de correo. No está limitada a vender automatización con IA.

El piloto está publicado en [egasis-piloto.onrender.com](https://egasis-piloto.onrender.com), con registro por invitación y **simulación** activa. También permite probar el circuito completo localmente. La guía de uso está en [GUIA_DEL_PILOTO.md](docs/GUIA_DEL_PILOTO.md), la instalación gratuita en [GRATIS.md](docs/GRATIS.md) y la evidencia conjunta en [ESTADO_ENTREGA.md](docs/ESTADO_ENTREGA.md). La facturación es opcional y el proceso continuo de envíos no inicia automáticamente. Las pruebas automatizadas usan bases temporales y proveedores simulados: no acreditan entregabilidad real, cobros reales ni capacidad de producción bajo carga.

## Inicio local

Requiere Python 3.13. Desde esta carpeta:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn egasis.app:app --host 127.0.0.1 --port 8765
```

Abrí [Egasis local](http://127.0.0.1:8765). Registrá un espacio y su propietario; la contraseña requiere al menos 12 caracteres. La API equivalente es `POST /api/register`, con `name`, `email`, `password` y, en producción, `invite`. El registro inicia una sesión por cookie. La base local se crea en `data/egasis.db`; la clave de desarrollo se conserva en `data/.development-key` con permisos privados.

Para procesar campañas continuamente, abrí otra terminal con el mismo entorno:

```sh
source .venv/bin/activate
python -m egasis.worker
```

El servidor web y el worker son procesos separados. El worker respeta campañas activas, días, horarios, límites, presupuesto y bajas. En simulación no transmite correo; la interfaz también permite ejecutar un ciclo simulado. Para envíos reales deben configurarse una cuenta SMTP/IMAP, un origen público HTTPS y `EGASIS_SIMULATION=false` en ambos procesos. Activar una campaña autoriza su primer mensaje configurado; las respuestas usan la política de revisión o automática de esa campaña.

## Configuración

Las variables se leen del entorno del proceso. No se carga automáticamente ningún archivo `.env`. Servidor y worker deben compartir la misma base, clave y configuración de simulación.

| Variable | Uso / valor inicial |
| --- | --- |
| `EGASIS_DATABASE_URL` | SQLite local por defecto; PostgreSQL para web y worker en servicios distintos. También acepta `DATABASE_URL` si no está definida la primera. |
| `EGASIS_SECRET_KEY` | Clave estable de cifrado. En producción, al menos 32 caracteres variados. Perderla impide descifrar las conexiones guardadas. |
| `EGASIS_PUBLIC_URL` | Origen público, inicialmente `http://127.0.0.1:8765`. HTTPS obligatorio en producción y para correo real. |
| `EGASIS_SIMULATION` | `true` por defecto. Admite `true` o `false`; `false` habilita correo real. |
| `EGASIS_ENV` | `production` exige base explícita, clave válida y HTTPS; solo admite registro con invitación. |
| `EGASIS_REGISTRATION_TOKEN` | Código de invitación para crear espacios en producción. Si falta, el registro en producción queda cerrado. |
| `EGASIS_GEMINI_API_KEY` | Clave Gemini opcional del servidor. Puede configurarse una conexión propia dentro de cada espacio. |
| `EGASIS_GEMINI_MODEL` | Modelo para las operaciones de IA. Consultá el valor inicial en `egasis/intelligence.py`. |
| `EGASIS_GEMINI_INPUT_PRICE` | USD por millón de tokens de entrada; obligatorio al cambiar a un modelo distinto del inicial. |
| `EGASIS_GEMINI_OUTPUT_PRICE` | USD por millón de tokens de salida; obligatorio al cambiar a un modelo distinto del inicial. |

El correo inicial configurado y la operación manual no requieren IA ni Stripe. La IA sí necesita una conexión válida y presupuesto disponible. Sus costos se calculan con tokens reportados y tarifas configuradas; no constituyen la factura del proveedor. Los resultados de consumo inciertos retienen una reserva para evitar gastar como si la llamada hubiera sido gratis.

Los estados de correo distinguen simulación, envío confirmado por SMTP, fallo e incertidumbre. Si la conexión se pierde durante la aceptación del mensaje, el envío queda incierto y no se reintenta automáticamente: debe contrastarse con el buzón/proveedor. SMTP aceptado no equivale a entrega en la bandeja principal. Las reuniones requieren una confirmación registrada; compartir un enlace no acredita una reserva.

## Facturación opcional

El piloto funciona sin una suscripción. Los precios comerciales de referencia son Starter **USD 299**, Growth **USD 799** y Agency **USD 1299** por mes. Estos nombres no imponen cuotas distintas en esta versión; los límites operativos se configuran por campaña, cuenta y espacio. El importe que se cobra lo define el Price configurado en Stripe, que debe coincidir con la oferta publicada.

Para habilitar Checkout y el portal, configurá:

```text
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
STRIPE_PRICE_STARTER
STRIPE_PRICE_GROWTH
STRIPE_PRICE_AGENCY
EGASIS_PUBLIC_URL
```

Creá tres precios recurrentes mensuales en Stripe, vinculá sus IDs a las variables y habilitá el Customer Portal en esa cuenta. Usá primero credenciales de prueba. El propietario puede abrir `POST /api/billing/checkout/{starter|growth|agency}` o `POST /api/billing/portal`; `GET /api/billing` informa variables faltantes sin exponer sus valores.

Registrá `https://TU-DOMINIO/api/billing/webhook` en Stripe para `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated` y `customer.subscription.deleted`. El endpoint verifica la firma sobre el cuerpo original con el SDK oficial, persiste el ID de cada evento y reconcilia la suscripción vigente consultando Stripe. Un fallo de consulta deja el evento disponible para reintento. Solo una sesión Checkout registrada por el servidor puede vincular la suscripción a un espacio; los metadatos recibidos no reasignan clientes. El retorno a la página de éxito no activa por sí solo una suscripción. [Referencia de webhooks y orden de eventos](https://docs.stripe.com/webhooks?lang=python).

## Importación de versiones anteriores

Se reconocen bases SQLite de Leadgen Studio y Nexus. Primero registrá un espacio de destino en la aplicación nueva. Usá el `workspace_id` informado por el registro. La importación abre el archivo anterior mediante URI de solo lectura y **nunca lo modifica**.

```sh
python -m egasis.migrate_legacy \
  --source /ruta/absoluta/outreach.db \
  --target-workspace 1 \
  --database-url sqlite:////ruta/absoluta/egasis/data/egasis.db
```

La ejecución anterior solo informa cantidades y revierte todos los cambios al destino. Para aplicarlos, repetí exactamente el comando agregando `--apply`. Se rechaza usar el mismo archivo como origen y destino. Si Nexus contiene varios clientes, agregá `--source-client ID`; cada cliente debe migrarse a su espacio correspondiente.

Se importan contactos y conversaciones en campañas **borrador**; los contactos y mensajes quedan como `imported`, sin trabajos de envío ni métricas de entregas nuevas. Las bajas y bloqueos históricos se preservan. No se importan contraseñas, claves API, cuentas de envío ni reuniones que carecen de una reserva verificada. Los textos de campaña disponibles en Nexus se conservan para revisión. El mismo origen, en la misma ruta canónica y espacio, puede importarse otra vez sin duplicar sus registros; conservar esa ruta es parte de la identidad de la importación. Los contactos históricos deben revisarse antes de incorporarlos a campañas nuevas.

## Despliegue en un servidor

El contenedor usa un usuario sin privilegios. La imagen no incluye bases ni claves del equipo: copia exclusivamente dependencias, código y archivos estáticos.

```sh
docker build -t egasis:local .
docker volume create egasis-data
docker run -d --name egasis-web --restart unless-stopped \
  --env-file /ruta/privada/egasis.env \
  -p 127.0.0.1:8765:8765 -v egasis-data:/app/data egasis:local
docker run -d --name egasis-worker --restart unless-stopped --no-healthcheck \
  --env-file /ruta/privada/egasis.env \
  -v egasis-data:/app/data egasis:local python -m egasis.worker
```

El archivo privado debe definir al menos `EGASIS_ENV=production`, una `EGASIS_SECRET_KEY` larga y aleatoria, `EGASIS_PUBLIC_URL=https://TU-DOMINIO`, `EGASIS_DATABASE_URL=sqlite:////app/data/egasis.db`, `EGASIS_SIMULATION=true` para la validación inicial y un `EGASIS_REGISTRATION_TOKEN` privado. No se debe incluir ese archivo en control de versiones. Configurá un proxy HTTPS hacia el puerto local 8765. La ruta de salud es `/api/health`.

El despliegue documentado usa SQLite y un servidor con disco persistente. No uses una carpeta de red ni varias máquinas sobre ese mismo archivo. Conservá copias consistentes de la base junto con la clave de cifrado, guardada por separado; no alcanza con copiar solo el archivo principal mientras SQLite escribe su WAL. Probá la restauración y supervisá servidor y worker. El contenedor y los proveedores externos deben validarse en el destino: no se despliegan ni se conectan por ejecutar las pruebas locales.

## Pruebas

```sh
python -m pip install pytest==9.1.1
python -m pytest -q
```

Para la parte de cobros y migración:

```sh
python -m pytest tests/test_billing.py tests/test_migration.py -q
```

Las pruebas de facturación verifican firma real del SDK, separación de espacios, propietario, duplicados durables, eventos fuera de orden y reintentos sin hacer llamadas a Stripe. Las de migración usan archivos temporales y verifican simulación, repetición, bajas, separación de clientes y ausencia de cambios al origen.

## Investigación, agenda y aprendizajes

Los contactos pueden ingresarse por CSV o mediante el conector Apollo con clave propia cifrada y `EGASIS_APOLLO_INTEGRATION_AUTHORIZED=true`. La búsqueda presenta resultados sin correos; obtener e importar un contacto requiere una acción explícita que puede consumir créditos de Apollo. La habilitación de la integración no sustituye el permiso correspondiente del proveedor.

La investigación consulta el sitio del contacto, conserva evidencia y, con Gemini disponible, evalúa el encaje y prepara borradores. Los aprendizajes se guardan primero en borrador: el propietario los aprueba para su uso por IA y puede archivarlos. El contexto tiene un límite visible en la guía y no incluye notas de otros espacios.

Las campañas pueden incorporar `{{booking_link}}` en sus plantillas. El contacto elige y confirma un horario en su enlace privado; la aplicación impide superponer reservas. Google Calendar es opcional, con conexión OAuth cifrada: consulta disponibilidad y concilia creación/cancelación del evento. La configuración actual de Google es acompañada, con tokens; no incluye un botón de autorización OAuth autoservicio. No se envían notificaciones de calendario a invitados.

Las fichas de reunión se descargan en Markdown y en PowerPoint editable, a partir de información registrada. [Revisión de las presentaciones](docs/MATERIALS_QA.md).

## Esquema y recuperación

El esquema vigente es la versión 3. Las bases anteriores requieren actualización explícita; no se cambian al iniciar sobre un esquema desactualizado. Para una base SQLite local nueva de Egasis:

```sh
python -m egasis.schema --database-url sqlite:////ruta/absoluta/egasis/data/egasis.db --upgrade
```

Antes de actualizar, creá y verificá una copia consistente. [Procedimiento SQLite](docs/RECOVERY.md); la restauración crea un destino nuevo y retiene los envíos hasta su revisión. [Pruebas con PostgreSQL real](docs/POSTGRES_QA.md) incluyen respaldo y restauración con sus herramientas nativas.

## Alcance que requiere validación externa

El piloto gratuito de Render y Neon ya está publicado. El 16 de septiembre se verificó el espacio del propietario creado por el usuario, con sesión conservada después de la noche y del despertar de Render Free. El recorrido público usó tres contactos ficticios: activación de campaña, primeros mensajes preparados y respuesta manual para Lucía procesada en simulación. El cierre mostró tres mensajes simulados, cero en cola, cero envíos reales, una respuesta y una reunión. La campaña y la cuenta ficticias quedaron pausadas. Sin Gemini, la interfaz mostró el error de conexión y conservó el texto de respuesta. También se confirmó una reserva de prueba de 30 minutos para Lucía; recargar su enlace mantuvo la misma confirmación. El panel mostró la reunión y su contexto de preparación con enlaces de ficha y PowerPoint, sin comprobar las descargas públicas. Tras un reinicio controlado confirmado en Render, se conservaron la sesión, los contactos, la conversación completa, los contadores, la campaña pausada, el perfil y la reunión confirmada.

El cierre y reingreso con contraseña no se verificó en este recorrido. La oferta, audiencia y firma del perfil quedaron vacías para que el propietario configure su negocio; la campaña conserva su oferta ficticia. Faltan esa configuración, las conexiones reales y el piloto con usuarios autorizados para medir utilidad, costo e intervención manual. El código y las pruebas locales no acreditan entregabilidad, permisos ni facturas de proveedores reales. No se contrató ningún servicio pago. Las versiones anteriores y sus bases permanecen preservadas; no se migraron datos reales.
