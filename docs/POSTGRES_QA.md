# Validación real de PostgreSQL

Fecha: 9 de septiembre de 2026.

## Resultado

La suite opcional `tests/test_postgres.py` aprobó **12 pruebas contra PostgreSQL 17.11 real**, con `psycopg2-binary 2.9.12`. El último pase completo terminó con `12 passed in 5.04s`.

No se encontraron defectos de PostgreSQL que requirieran modificar `schema.py` o `store.py`. Se validó la versión de esquema 3, incluidos los pasos explícitos por las versiones 1 y 2.

## Aislamiento utilizado

- Binarios: `/opt/homebrew/opt/postgresql@17/bin`.
- Clúster exclusivo de QA: `/private/tmp/egasis-pg-qa-lbVSQKG5/data`.
- Escucha: `127.0.0.1:55479` y socket Unix dentro del mismo directorio temporal.
- Rol sintético: `egasis_qa`.
- Base de coordinación vacía: `egasis_test_qa_lbvsqkg5`.
- Cada prueba creó bases hijas con nombres `egasis_test_` más un identificador aleatorio. El cierre de cada fixture eliminó únicamente las bases que ese fixture había creado.

No se utilizó el clúster por defecto de Homebrew ni ninguna base existente del usuario. Los datos, claves de cifrado y contraseñas de prueba fueron sintéticos. Las pruebas bloquearon los transportes SMTP, IMAP y HTTP externo. La prueba de IA utilizó una respuesta HTTP simulada y un presupuesto persistido en PostgreSQL.

El entorno aislado bloqueó la memoria compartida de PostgreSQL y las conexiones a loopback. Se usaron las aprobaciones de ejecución exactas para inicializar, iniciar, consultar, probar y apagar exclusivamente este clúster temporal. Una comprobación aislada de `pg_ctl status` informó erróneamente que no había servidor porque no podía inspeccionar el proceso externo. La comprobación fuera del aislamiento confirmó el mismo proceso original, PID 12114.

## Cobertura ejecutada

1. Guardas que rechazan hosts externos, el puerto por defecto 5432, bases sin el prefijo de pruebas y parámetros de URL que podrían cambiar el destino.
2. Inicialización de una base vacía con manifiesto de migraciones y correspondencia entre tablas/columnas reales y modelos ORM. Inserciones con identificadores generados y booleanos PostgreSQL.
3. Migración explícita v1 → v2 → v3, conservación de filas y valores iniciales de los nuevos campos. El arranque rechaza una actualización implícita.
4. Adopción explícita del esquema legado v2 sin manifiesto. El fixture utiliza el formato legado admitido, no un esquema v3 creado fuera del sistema de migraciones.
5. Reversión conjunta de DDL y manifiesto cuando una validación de migración falla deliberadamente.
6. Inicialización concurrente de una base vacía, serializada mediante el bloqueo de migraciones.
7. Registro y autenticación por la API, separación de espacios, cuenta cifrada y campaña con entrega simulada. La métrica de enviados reales permanece en cero.
8. Dos solicitudes HTTP concurrentes de respuesta que producen un único mensaje y un único trabajo de salida.
9. Dos workers concurrentes que respetan un límite diario de cuenta de un mensaje.
10. Dos workers concurrentes que respetan un límite diario de campaña de un mensaje.
11. Reserva concurrente del presupuesto de IA: una segunda generación no puede gastar el saldo ya reservado por la primera.
12. `pg_dump` en formato custom y restauración mediante `pg_restore` en otra base temporal vacía. Se comprobaron el manifiesto, las cantidades de filas de todas las tablas, datos de contacto, descifrado de la cuenta y generación de identificadores posteriores a la restauración.

## Ejecución opcional

Sin configuración explícita, la suite no se conecta a PostgreSQL:

```text
python3 -m pytest tests/test_postgres.py -q
12 skipped
```

El pase real utilizó esta URL de QA, que ya no responde porque el clúster quedó apagado:

```text
EGASIS_TEST_POSTGRES_URL=postgresql+psycopg2://egasis_qa@127.0.0.1:55479/egasis_test_qa_lbvsqkg5 python3 -m pytest tests/test_postgres.py -q
12 passed in 5.04s
```

Para repetir la suite, se debe proporcionar otra base local temporal con permisos de creación de bases. `EGASIS_TEST_POSTGRES_BIN` permite indicar la carpeta de `pg_dump` y `pg_restore` compatibles. Las pruebas no inicializan ni arrancan un servidor por su cuenta y nunca seleccionan automáticamente una base de aplicación.

## Cierre comprobado

Antes del apagado, una consulta a `pg_database` devolvió únicamente `egasis_test_qa_lbvsqkg5` entre los nombres de QA: no quedaron bases hijas de los tests.

Se ejecutó el apagado exacto:

```text
/opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /private/tmp/egasis-pg-qa-lbVSQKG5/data -m fast -w stop
waiting for server to shut down.... done
server stopped
```

El log registró `database system is shut down` a las 21:40:18 del 9 de septiembre de 2026, hora de Argentina. Se comprobó además la ausencia de `data/postmaster.pid` y del socket `.s.PGSQL.55479` dentro del directorio temporal. El clúster no quedó ejecutándose. Sus archivos temporales y la base de coordinación vacía pueden conservarse como diagnóstico y no forman parte de los datos del producto.

## Límite de esta evidencia

Esta validación comprueba comportamiento funcional, migraciones, bloqueos y restauración en una instancia PostgreSQL 17 local. No mide capacidad bajo carga de producción ni valida un proveedor administrado, TLS remoto o credenciales reales. Tampoco envía correos ni ejecuta pagos o generaciones de IA reales.
