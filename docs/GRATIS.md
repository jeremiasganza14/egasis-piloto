# Egasis gratis por ahora: Render Free + Neon Free

La preferencia vigente es **costo de alojamiento USD 0, sin tarjeta, compras ni upgrades**. Esta preparación permite publicar la aplicación completa —interfaz y API— como piloto en simulación. El archivo [render.yaml](../render.yaml) está listo; esta guía no acredita que exista ya un despliegue público.

## Qué incluye esta opción

Un único servicio web Render **Free** sirve Egasis y una base PostgreSQL Neon **Free** conserva sus datos. No se crea una base de Render, un disco, un worker, un cron ni un servicio de pago. Se usa el subdominio del proveedor; no hace falta comprar dominio. Los despliegues automáticos están desactivados en la plantilla.

La interfaz permite trabajar con contactos, campañas, conversaciones de ejemplo, aprendizajes, materiales y reuniones. El botón de ciclo de simulación avanza las pruebas cuando lo solicita la persona responsable. No hay motor continuo ni correo real. Para mantener costo cero, el piloto comienza sin conectar Stripe, IA, Apollo, correo o calendario externos: sus cuentas, permisos y consumos son independientes de este alojamiento.

## Límites que aceptamos para el piloto

Render Free duerme tras 15 minutos sin tráfico; volver a abrir la web puede requerir alrededor de un minuto. Su disco es efímero y bloquea SMTP saliente en los puertos 25, 465 y 587. Ofrece 750 horas gratuitas por workspace y mes, compartidas por sus servicios gratuitos. También limita transferencia y construcción; sin medio de pago, agotar esas cuotas puede suspender servicios o nuevas compilaciones. Un tráfico saliente elevado puede causar suspensión. **No usamos Render PostgreSQL Free porque expira a los 30 días.** [Límites oficiales de Render Free](https://render.com/docs/free).

Neon Free no exige tarjeta ni tiene un plazo de prueba: incluye **0,5 GB de almacenamiento y 100 CU-h mensuales por proyecto**. El cómputo puede suspenderse al quedar inactivo. Las cuotas y los arranques en frío limitan este piloto; revisar el consumo en el panel y mantener pocos datos de prueba. “Sin plazo de prueba” no significa capacidad ilimitada ni una garantía futura sobre las condiciones del proveedor. [Plan Free de Neon](https://neon.com/pricing).

No agregar una tarjeta ni elegir un plan de pago para resolver límites. Si una cuenta exige verificación o un upgrade, dejar ese paso pendiente. Esta opción no promete campañas reales funcionando las 24 horas.

## Preparación en los paneles

### 1. Código y base

1. Ingresar a las cuentas de GitHub, Neon y Render. No compartir contraseñas en el chat. Si todavía no hay sesión, ese ingreso es el siguiente paso necesario.
2. Guardar el código en un repositorio al que Render pueda acceder. Su raíz debe contener `render.yaml`, `.python-version`, `requirements.txt`, `egasis/` y `static/`. En este workspace, esa carpeta es `/Users/jereganza/Desktop/Egasis_Workspace/egasis`. No incluir `.env`, bases locales, archivos de clientes ni claves; revisar el contenido antes de publicar el repositorio.
3. Crear un proyecto **Free** en Neon. Elegir PostgreSQL 17, que se probó localmente, y una región cercana a la web. Crear una base vacía para este piloto. No importar las bases anteriores.
4. Copiar desde el panel de Neon la conexión PostgreSQL **directa**, conservando `sslmode=require` y los demás parámetros de seguridad que entregue. La conexión contiene una contraseña: se pega únicamente en el gestor privado de variables de Render.

### 2. Un solo servicio web gratuito

En Render, crear un Blueprint desde el repositorio y seleccionar `render.yaml`. Revisar el resumen antes de aplicarlo: debe mostrar únicamente `egasis-piloto`, tipo web, plan **Free**. La plantilla solicita `EGASIS_DATABASE_URL`; pegar allí la conexión privada de Neon. No agregar Render Postgres ni otros recursos.

La plantilla establece estas variables:

La región de la web se fija en Ohio, igual que la base Neon del piloto. Después de crear el Blueprint, desactivar también **Settings → Auto Sync**. `autoDeployTrigger: off` evita despliegues por cambios de código, pero la sincronización automática del Blueprint es una opción separada.

| Variable | Configuración |
| --- | --- |
| `EGASIS_ENV` | `production` |
| `EGASIS_SIMULATION` | `true` |
| `EGASIS_DATABASE_URL` | Conexión privada de Neon con TLS; se completa en el panel. |
| `EGASIS_SECRET_KEY` | Se genera en Render; conservar una copia protegida y estable. |
| `EGASIS_REGISTRATION_TOKEN` | Invitación privada generada en Render para crear el propietario. |

La clave estable permite descifrar las conexiones del espacio: no regenerarla al redesplegar. Después de crear el propietario, se puede vaciar la invitación en el panel y redesplegar para cerrar nuevos registros. Al volver a sincronizar el Blueprint, comprobar que el registro siga cerrado.

Render genera los secretos ausentes mediante `generateValue`; `sync: false` solicita la conexión durante la creación inicial. No hay secretos incluidos en el archivo. [Referencia de Blueprints](https://render.com/docs/blueprint-spec).

El arranque exporta `EGASIS_PUBLIC_URL` desde `RENDER_EXTERNAL_URL`, salvo que se haya configurado expresamente otro origen HTTPS. No se modifica `Settings` ni se guarda una URL provisional. Render suministra `RENDER_EXTERNAL_URL` y `PORT`; la aplicación escucha el puerto recibido. [Variables de Render](https://render.com/docs/environment-variables).

El archivo `.python-version` selecciona la familia Python 3.13; Render resuelve su versión de mantenimiento disponible. [Versión de Python](https://render.com/docs/python-version).

### 3. Arranque y comprobación

El build instala `requirements.txt`. El arranque ejecuta primero esta comprobación y solo inicia la web si pasa:

```sh
python -m egasis.deployment_check --target render --role web
```

El comprobador no abre la base ni la red y no imprime los valores de las variables. Exige configuración de producción, origen HTTPS y PostgreSQL externo con TLS. `configuration_ready: true` valida esa configuración, no la disponibilidad de Neon ni el plan de las cuentas. Rechaza un worker alojado en Render Free.

En una base completamente vacía, el primer arranque de Egasis crea el esquema versionado actual. Una base con versión antigua requiere la actualización explícita de la sección siguiente; la aplicación no la modifica silenciosamente.

Cuando el servicio esté listo:

1. Abrir su dirección HTTPS y comprobar que `/api/health` responde.
2. Crear el espacio con la invitación del panel, sin publicarla, e ingresar como propietario.
3. Confirmar que la interfaz indique simulación. Cargar los datos de ejemplo.
4. Revisar una campaña, activar sus días y horario de prueba y procesar un ciclo simulado desde la interfaz. Comprobar el mensaje y su estado **simulado**; no debe llegar correo a nadie.
5. Cerrar y volver a abrir la sesión. Comprobar que los contactos y cambios siguen presentes. Repetir después de un reinicio del servicio: deben seguir en Neon.
6. Registrar la dirección pública y el resultado del recorrido; recién entonces se puede afirmar que el piloto está publicado y probado. Seguir la [guía de uso y medición](GUIA_DEL_PILOTO.md).

## Actualizaciones y conservación de datos

Esta plantilla no migra una base existente durante el build. Antes de actualizar una versión del esquema, detener la web y cualquier worker, conservar un respaldo y ejecutar desde un equipo de mantenimiento con las dependencias instaladas y las variables privadas del piloto ya cargadas:

```sh
python -c 'from sqlalchemy import create_engine; from egasis.settings import Settings; from egasis.schema import upgrade_schema; config=Settings.from_env(); engine=create_engine(config.database_url); print(upgrade_schema(engine)); engine.dispose()'
```

Ese comando **sí conecta y modifica la base**. No contiene ni imprime la URL; no se ejecutó sobre Neon durante esta preparación. En el equipo local, definir explícitamente `EGASIS_PUBLIC_URL` con el origen Render: allí no existe `RENDER_EXTERNAL_URL` por defecto. No ejecutar adopciones de bases desconocidas ni apuntar a los archivos de las versiones anteriores. Una vez actualizada y comprobada la base, desplegar el código correspondiente y repetir el recorrido de simulación.

Conservar una copia protegida de los datos que importe mantener, además de la clave de cifrado. Un CSV de contactos no reemplaza un respaldo completo de usuarios, conversaciones y configuración. [Prueba local de respaldo y restauración PostgreSQL](POSTGRES_QA.md).

## Correo real en una etapa posterior: worker local

Como alternativa posterior, el proceso de envío puede correr en la computadora del propietario y usar la **misma base Neon, la misma clave y el mismo origen público**. La web seguiría en Render. Esta posibilidad requiere una configuración separada y pruebas de la cuenta SMTP/IMAP; no queda habilitada por el Blueprint.

El comando del proceso local, una vez configurado, es:

```sh
python -m egasis.worker
```

Solo trabaja mientras la computadora esté encendida, conectada y con el proceso abierto. Si duerme o se cierra, no envía ni lee nuevas respuestas hasta que vuelva. También consume la cuota de Neon. Antes de una transición real, pausar campañas, revisar trabajos de prueba y comprobar la configuración de simulación en ambos procesos. Un worker real no debe procesar por accidente campañas creadas para ensayar.

Render Free sigue bloqueando los puertos SMTP: poner allí el worker o desactivar la simulación no elimina ese bloqueo. Tampoco se puede presentar esta alternativa local como envío permanente gratuito. No se contrató ningún proveedor ni se activó correo real en esta preparación.
