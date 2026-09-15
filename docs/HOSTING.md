# Alojamiento de Egasis

Estado: preparación local verificada; **no se desplegó, compró ni conectó ningún servicio**. La preferencia vigente es alojamiento gratuito por ahora. La opción principal para el piloto público es [Render Free + Neon Free, en simulación](GRATIS.md): un solo servicio web, sin worker permanente ni tarjeta. Está preparado [render.yaml](../render.yaml).

Las alternativas de pago de esta guía se conservan como referencia para una etapa futura. **No hay un pago autorizado ni una autorización de pago pendiente de reiterar.** No se eligen upgrades para continuar el piloto gratuito.

## Referencia futura: alternativas de pago

Referencias verificadas el 9 de septiembre de 2026, para una operación pequeña y sin servicios ya contratados:

| Alternativa | Referencia mínima mensual | Alcance del importe |
| --- | --- | --- |
| Railway Pro: web + worker + PostgreSQL | USD 20 | Es un consumo mínimo de Railway, que cubre los primeros USD 20 de recursos. El consumo superior se cobra adicionalmente. |
| Vercel Pro: web; Railway Pro: worker + PostgreSQL | Desde USD 40 | Suma las referencias iniciales de Vercel Pro y Railway Pro. Uso adicional, miembros facturables y extras pueden elevar el total. |

Railway Pro publica USD 20 mensuales como mínimo aplicado al consumo. Vercel Pro publica una referencia inicial de USD 20 mensuales con crédito de uso; su esquema de miembros y recursos debe revisarse para la cuenta que se contrate. Estos valores no incluyen una estimación medida del consumo de Egasis, dominio, correo, IA, impuestos ni otras integraciones. [Facturación de Railway](https://docs.railway.com/pricing/understanding-your-bill), [precios de Vercel](https://vercel.com/pricing).

**Vercel Hobby no corresponde al lanzamiento comercial de Egasis:** sus condiciones lo reservan al uso personal no comercial; el uso comercial requiere Pro o Enterprise. No se plantea Vercel gratis como alojamiento de este SaaS. [Condiciones de uso de Vercel](https://vercel.com/docs/limits/fair-use-guidelines).

Si en el futuro se decide contratar un motor permanente, Railway con los tres servicios mantiene un solo proveedor y una red privada para la base. La alternativa mixta no elimina el costo del worker permanente. La decisión actual es seguir la [opción gratuita](GRATIS.md); esta referencia no autoriza ni ejecuta un cobro.

## Qué está preparado en el código

- `Settings.from_env()` acepta `EGASIS_DATABASE_URL` y, si falta, `DATABASE_URL`. Normaliza `postgres://` y `postgresql://` a `postgresql+psycopg2://`, conservando contraseñas escapadas y parámetros de conexión.
- `psycopg2-binary==2.9.12` está incluido en las dependencias. Se verificó su carga local sin abrir una conexión.
- Producción exige base explícita, clave de cifrado de al menos 32 caracteres variados y un origen HTTPS válido. No genera claves ni crea la carpeta `data` cuando usa PostgreSQL.
- La invitación de registro, si se configura, también requiere al menos 32 caracteres variados. Sin invitación, el registro queda cerrado y las cuentas existentes pueden ingresar.
- `EGASIS_SIMULATION` admite `true` y `false`; permanece en `true` por defecto.
- `python -m egasis.deployment_check` revisa la configuración sin red, conexión a base ni escritura local. No imprime URLs de conexión, claves ni sus valores.

Las pruebas de esta preparación y de compatibilidad con la API se ejecutan con:

```sh
python3 -m pytest tests/test_deployment.py tests/test_api.py -q
```

La preparación se verificó con pruebas locales. Además, [POSTGRES_QA.md](POSTGRES_QA.md) documenta 12 pruebas contra PostgreSQL 17.11 real, incluyendo esquema v3, concurrencia y respaldo/restauración. El entorno del proveedor todavía requiere validación.

## Railway: web + worker + PostgreSQL

Creá un proyecto y agregá PostgreSQL desde el panel. Creá después dos servicios del mismo código: `egasis-web` y `egasis-worker`. Ambos deben usar el mismo commit y la misma región que la base. Railway permite referenciar `DATABASE_URL` del servicio PostgreSQL desde los otros servicios. Su conexión pública es distinta de la privada y se utiliza para clientes externos. [PostgreSQL en Railway](https://docs.railway.com/databases/postgresql).

Usá como raíz del proyecto la carpeta que contiene `Dockerfile`, `requirements.txt`, `egasis/` y `static/`. En este workspace es `/Users/jereganza/Desktop/Egasis_Workspace/egasis`; si el repositorio tiene otra raíz, elegí esta subcarpeta como Root Directory en los dos servicios.

### Variables de los dos servicios

| Variable | Configuración |
| --- | --- |
| `EGASIS_ENV` | `production` |
| `EGASIS_DATABASE_URL` | `${{Postgres.DATABASE_URL}}`, si el servicio de base se llama `Postgres`; usá el selector de referencias para el nombre real. |
| `EGASIS_SECRET_KEY` | El mismo secreto aleatorio y estable en web y worker. Guardalo como variable privada compartida. |
| `EGASIS_PUBLIC_URL` | El origen HTTPS definitivo de la web, sin ruta ni parámetros. |
| `EGASIS_SIMULATION` | `true` durante la primera validación. |
| `EGASIS_REGISTRATION_TOKEN` | Invitación privada para crear el primer propietario; puede retirarse después para cerrar nuevos registros. |
| `PORT` | `8765` en el servicio web. |

**Sobrescribí `EGASIS_DATABASE_URL` explícitamente.** El Dockerfile trae `sqlite:////app/data/egasis.db` como valor local; esa variable tiene prioridad sobre `DATABASE_URL`. No alcanza con agregar solo `DATABASE_URL` a un contenedor que conserva el valor SQLite.

No hace falta un volumen de datos en los contenedores web y worker cuando usan PostgreSQL. La persistencia corresponde al servicio de base. Conservá una copia protegida de `EGASIS_SECRET_KEY`: cambiarla sin una migración de cifrado impide leer las conexiones guardadas. No pegues secretos en comandos, archivos de código ni conversaciones.

Configurá las integraciones opcionales únicamente cuando se vayan a probar: correo y conexiones por espacio, Gemini, Google Calendar y Stripe. Tener sus variables presentes no verifica permisos, facturación ni credenciales.

### Comandos y ajustes por servicio

| Ajuste | `egasis-web` | `egasis-worker` |
| --- | --- | --- |
| Construcción | Dockerfile incluido | El mismo Dockerfile |
| Start Command | `python -m uvicorn egasis.app:app --host 0.0.0.0 --port 8765` | `python -m egasis.worker` |
| Réplicas iniciales | 1 | 1 |
| Dominio público | Sí, destino puerto `8765` | Ninguno |
| Healthcheck HTTP en Railway | `/api/health` | Sin healthcheck HTTP; no sirve una web |
| Reinicio | On Failure | On Failure |
| Serverless / suspensión por inactividad | Desactivada durante el piloto | Desactivada: el proceso debe permanecer activo |

El Dockerfile contiene un healthcheck HTTP para la web. El worker no debe recibir ese control HTTP en la plataforma; se supervisa por estado del proceso, registros y avance de trabajos. En Docker directo se inicia con `--no-healthcheck`, como muestra el README.

Primero configurá el dominio y las variables. Después ejecutá la comprobación correspondiente desde el entorno que recibirá esas variables:

```sh
python -m egasis.deployment_check --target railway --role web
python -m egasis.deployment_check --target railway --role worker
```

Con configuración válida y simulación activa, ambos comandos deben terminar con código `0` y devolver `"configuration_ready": true`. El código `2` indica errores de configuración. El resultado `"scope": "offline_configuration_only"` recuerda que no se contactó Railway, PostgreSQL ni ningún otro proveedor.

### Migración de la base antes de iniciar

La actualización de esquema debe ejecutarse una sola vez por despliegue, con servidor y worker detenidos o en una ventana de mantenimiento. Para una base nueva, o una base ya versionada, este es el comando de migración:

```sh
python -c 'from sqlalchemy import create_engine; from egasis.settings import Settings; from egasis.schema import upgrade_schema; config=Settings.from_env(); engine=create_engine(config.database_url); print(upgrade_schema(engine)); engine.dispose()'
```

El comando lee la conexión del entorno y solo imprime el estado del esquema. A diferencia del comprobador anterior, **este comando sí conecta y modifica la base**. Se incluye para su ejecución posterior en el servidor; no se ejecutó contra servicios externos durante esta preparación.

Podés usarlo como Pre-Deploy Command del servicio web, manteniendo un único responsable de migrar. No lo dupliques en el worker. Railway ejecuta los comandos previos al despliegue en un contenedor separado, con variables y acceso a la red privada; si fallan, el despliegue no continúa. No uses ese mecanismo para escribir archivos que deban persistir en el contenedor web. [Comandos previos al despliegue](https://docs.railway.com/deployments/pre-deploy-command).

Una base Egasis anterior sin manifiesto requiere una adopción explícita y copia de seguridad previa:

```sh
python -c 'from sqlalchemy import create_engine; from egasis.settings import Settings; from egasis.schema import upgrade_schema; config=Settings.from_env(); engine=create_engine(config.database_url); print(upgrade_schema(engine, adopt_unversioned=True)); engine.dispose()'
```

La adopción acepta únicamente el esquema Egasis conocido. No convierte una base de Leadgen Studio o Nexus: esas bases se importan con el procedimiento separado del README. No dejes la adopción activada como una reparación automática para cualquier error de esquema.

Después de migrar, iniciá la web, verificá `/api/health`, creá el propietario con la invitación y comenzá el worker en simulación. Para consultar el estado del esquema sin actualizarlo:

```sh
python -c 'from sqlalchemy import create_engine; from egasis.settings import Settings; from egasis.schema import schema_status; config=Settings.from_env(); engine=create_engine(config.database_url); print(schema_status(engine)); engine.dispose()'
```

La ruta de salud confirma que responde la aplicación; no demuestra que el worker esté ejecutándose ni que SMTP funcione. Verificá un ciclo simulado y su resultado visible en Egasis.

### Correo real y plan de Railway

Railway habilita SMTP saliente únicamente en **Pro o superior**. Free, Trial y Hobby requieren proveedores de correo mediante API HTTPS. El motor actual de Egasis envía por SMTP, de modo que un worker con correo real necesita Pro o un alojamiento externo que permita ese tráfico. Pasar a Pro requiere volver a desplegar el servicio para aplicar el cambio de red. No se contrató ningún plan. [Red saliente y correo en Railway](https://docs.railway.com/networking/outbound-networking).

Cuando el plan y las cuentas estén comprobados, configurá `EGASIS_SIMULATION=false` en web y worker y ejecutá:

```sh
python -m egasis.deployment_check --target railway --role worker --railway-plan pro
```

`--railway-plan pro` es una declaración del operador: no compra un plan, no consulta la cuenta y no demuestra conectividad. Antes de activar campañas, completá una prueba real controlada con la cuenta remitente y verificá recepción, respuesta, baja y límites. IMAP y los permisos del proveedor de correo también requieren validación independiente.

No se agregó un `railway.toml` o `railway.json` nuevo: la documentación consultada marca Config as Code como obsoleto y prevé que sus archivos heredados dejen de funcionar el 1 de diciembre de 2026. Los ajustes de esta guía se cargan en el panel; si se adopta Infrastructure as Code, debe usarse su esquema vigente. [Configuración de Railway](https://docs.railway.com/config-as-code/reference).

## Vercel: web y API, con worker externo

La alternativa es alojar **la aplicación FastAPI completa y sus archivos estáticos en Vercel**, mientras `python -m egasis.worker` sigue en Railway u otro servidor permanente. Los dos procesos acceden al mismo PostgreSQL. Mantener interfaz y API bajo el mismo origen conserva el comportamiento de las cookies y evita requerir cambios de CORS.

Vercel soporta FastAPI y permite seleccionar el módulo mediante `tool.vercel.entrypoint`. Para esta alternativa, agregá este contenido a un `pyproject.toml` en la raíz de Egasis, conservando cualquier configuración que ya exista:

```toml
[tool.vercel]
entrypoint = "egasis.app:app"

[tool.vercel.fastapi.static]
cdn = false
exclude = false
```

La opción estática mantiene los archivos y sus respuestas bajo el middleware de la aplicación. No cambies el punto de entrada a `egasis.worker`. Conservá `requirements.txt`; un archivo `.python-version` con `3.13` indica el runtime esperado, sujeto al soporte del proveedor. [FastAPI en Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [runtime de Python](https://vercel.com/docs/functions/runtimes/python).

En un `.vercelignore`, excluí al menos:

```text
data/
.env
.env.*
*.env
.venv/
venv/
__pycache__/
**/__pycache__/
*.db
*.db-*
*.sqlite*
.pytest_cache/
.git/
```

`.dockerignore` protege el contexto Docker; no debe suponerse que controla el paquete de Vercel. Los archivos de configuración Vercel anteriores son instrucciones revisables de esta alternativa y todavía no se agregaron como configuración activa.

Usá `EGASIS_ENV=production`, la misma clave de cifrado que el worker, el origen público Vercel en `EGASIS_PUBLIC_URL` y una conexión PostgreSQL accesible desde Vercel con `sslmode=require` o verificación TLS más estricta. Si PostgreSQL está en Railway, su dominio `.railway.internal` no funciona desde Vercel: necesitás la conexión pública habilitada por el proveedor. No publiques esa URL ni su contraseña. El worker dentro de Railway puede conservar su conexión privada a la misma base.

Ejecutá la migración desde el entorno externo de mantenimiento antes de habilitar la web. El módulo web abre la base al inicializarse; la base y su esquema deben estar disponibles cuando Vercel cargue la aplicación. No hagas migraciones automáticas durante cada petición ni durante un build que apunte a datos de producción.

La comprobación local de esta variante es:

```sh
python -m egasis.deployment_check --target vercel --role web
```

El comprobador rechaza SQLite, direcciones privadas de Railway y conexiones PostgreSQL externas sin TLS explícito. También rechaza `--target vercel --role worker`.

### Límites concretos de la alternativa

- Las funciones tienen duración máxima; un bucle permanente de worker no corresponde a ese modelo. El filesystem de la función es de solo lectura salvo espacio temporal, que no sirve como base persistente. [Runtimes de Vercel](https://vercel.com/docs/functions/runtimes).
- Operaciones de IA, consulta de calendario o sincronización manual de correo deben terminar dentro del límite de la función contratado. Su duración real todavía debe medirse allí.
- La inicialización inspecciona el esquema y abre conexiones de base. Hay que medir arranque en frío y concurrencia; esta entrega no configura un pool externo ni acredita capacidad de muchas instancias simultáneas.
- Los límites de intentos de ingreso de la API viven en memoria del proceso. Una instalación con múltiples instancias necesita además controles de acceso y limitación en el perímetro; esos contadores no son globales entre funciones.
- Preview y producción deben usar bases y secretos distintos. Una previsualización no debe recibir conexiones capaces de enviar correos ni operar sobre clientes de producción.
- Si el worker externo usa Railway y SMTP real, conserva el requisito Pro o superior. Usar Vercel para la web no cambia las restricciones de red de ese worker.

## Resultado verificable y pendientes

Listo localmente: normalización PostgreSQL, dependencia instalada disponible, configuración de producción sin escritura local, preflight sin secretos y pruebas de regresión con la API.

Pendiente de un entorno elegido: crear los servicios y dominio; cargar secretos en su gestor; ejecutar y verificar migraciones contra PostgreSQL; verificar build y arranque; restaurar una copia de seguridad; probar worker, correo, permisos e integraciones reales; medir concurrencia y límites. Ninguno de esos resultados se infiere de que el preflight devuelva `configuration_ready`.
