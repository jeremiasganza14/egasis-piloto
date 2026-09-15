# Copias y recuperación de Egasis

Estos comandos se ejecutan desde la carpeta del proyecto, con el mismo Python que tiene instaladas sus dependencias. Todas las rutas se indican explícitamente: la herramienta no carga `.env`, no busca la base activa y no lee archivos con claves.

## Crear una copia consistente

Creá primero la carpeta donde guardarás las copias. Elegí un nombre nuevo para cada ejecución.

```sh
mkdir -p backups
python3 -m egasis.backup create --source data/egasis.db --output backups/egasis-2026-09-09.sqlite
```

Se generan dos archivos que deben conservarse juntos:

- `backups/egasis-2026-09-09.sqlite`: instantánea de la base.
- `backups/egasis-2026-09-09.sqlite.manifest.json`: checksum SHA-256, tamaño, fecha y versión de esquema.

La copia usa la API de respaldo de SQLite e incluye las transacciones confirmadas que estén en su WAL. Puede realizarse mientras la aplicación funciona; no se incluyen transacciones sin confirmar. No alcanza con copiar solamente el archivo principal de una base activa. Los archivos de respaldo se crean con permisos `0600` y nunca reemplazan un destino existente.

La instantánea contiene los datos del sistema y las credenciales almacenadas en forma cifrada. Conservá **la misma `EGASIS_SECRET_KEY` por separado**, en su almacenamiento seguro habitual: será necesaria para descifrarlas al recuperar. La herramienta no incluye esa clave ni el archivo `.development-key`.

## Verificar una copia

```sh
python3 -m egasis.backup verify --backup backups/egasis-2026-09-09.sqlite
```

La verificación comprueba checksum, tamaño, integridad SQLite, relaciones entre registros y esquema conocido de Egasis. Un resultado correcto contiene `"valid": true`. También se rechazan copias con archivos auxiliares `-wal`, `-shm` o `-journal`, ya que el respaldo debe ser independiente. El checksum detecta alteraciones; no autentica una copia recibida de un origen desconocido.

Una base válida de una versión anterior puede respaldarse antes de migrarla. Si el resultado indica `upgrade_required` o `unversioned`, seguirá necesitando la migración o adopción explícita correspondiente antes de iniciar la aplicación. La herramienta de respaldo no modifica el esquema.

## Restaurar a un archivo nuevo

```sh
python3 -m egasis.backup restore --backup backups/egasis-2026-09-09.sqlite --output data/egasis-recuperada-2026-09-09.sqlite
```

El destino debe ser nuevo y su carpeta debe existir. No se reemplaza la base en uso ni se modifica el respaldo original. La copia se verifica antes de restaurar y el archivo restaurado vuelve a pasar controles de integridad.

Antes de cambiar la aplicación a este archivo:

1. Detené tanto el servidor web como todos los workers.
2. Conservá la base anterior para poder volver a examinarla.
3. Configurá `EGASIS_DATABASE_URL` en ambos procesos para que apunte al archivo restaurado, usando una ruta absoluta. Mantené la clave de cifrado original.
4. Revisá el estado del esquema con el procedimiento de migración documentado antes de arrancar una versión diferente de Egasis.
5. Iniciá la aplicación y volvé a iniciar sesión. Revisá cuentas, conversaciones y envíos retenidos antes de reactivar campañas.

La restauración aplica estas medidas automáticamente:

| Registros | Estado al recuperar |
| --- | --- |
| Campañas activas | Pausadas |
| Cuentas de correo | Desactivadas |
| Sesiones iniciadas | Revocadas |
| Contactos pendientes de primer envío | `recovery_hold` |
| Respuestas recibidas todavía sin procesar | Pendientes de revisión |
| Mensajes salientes sin constancia de envío en la copia | `uncertain`, con un trabajo de revisión |
| Mensajes ya aceptados por SMTP en la copia | Conservan su estado y fecha |

Una instantánea no permite saber qué ocurrió **después de su fecha**. Incluso un trabajo que figuraba como pendiente pudo haberse enviado posteriormente. Por eso no se reanudan colas ni contactos retenidos automáticamente y tampoco se borran sus claves de idempotencia.

Los envíos `uncertain` se concilian con registros del proveedor: para marcarlos como aceptados se requiere evidencia y una fecha UTC verificable; si se confirma que no fueron aceptados, quedan como fallidos y no se reintentan automáticamente. La CLI no ofrece una liberación masiva de contactos `recovery_hold`: requieren revisión operativa individual antes de una futura prospección. Los eventos posteriores a la instantánea necesitan otras fuentes para recuperarse.

## Recuperación del correo durante la operación

El worker exige una sincronización IMAP completa de la cuenta antes de cada envío real. Si IMAP falla o todavía hay más de 200 mensajes nuevos por procesar, el envío queda pendiente **sin consumir un intento ni reservar cuota**. Las siguientes sincronizaciones completan el trabajo; una cuenta bloqueada no impide procesar otras cuentas.

Si aparece una respuesta más reciente, se cancela la respuesta preparada para el mensaje anterior. Una desconexión durante el envío de datos SMTP deja el resultado como incierto; no se hace un reenvío automático que pueda duplicarlo. Al reiniciar el worker, las entregas interrumpidas también quedan retenidas para conciliación.

Al reiniciar una campaña detenida, solo se reconstruyen primeros mensajes cancelados que nunca se intentaron, no tienen reserva ni identificador de proveedor, conservan su cuenta original y no recibieron respuesta. Los mensajes con indicios de envío permanecen fuera de esta reactivación.

## Comprobar el procedimiento sin datos reales

```sh
python3 -m pytest tests/test_backup.py tests/test_mail.py tests/test_worker.py -q
```

Las pruebas usan bases temporales y servicios de correo simulados. Cubren respaldo con WAL activo, restauración retenida, rechazo de archivos alterados y destinos existentes, bloqueo por IMAP, recuperación de entregas inciertas y reactivación de colas sin duplicar envíos.
