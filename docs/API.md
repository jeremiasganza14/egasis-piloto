# API de Egasis

Mapa generado del esquema OpenAPI de la aplicación. Los permisos se aplican en cada ruta; los endpoints de reserva/baja y el webhook tienen sus verificaciones específicas. El servidor web no inicia el worker.

| Método | Ruta | Operación |
| --- | --- | --- |
| GET | `/` | Index |
| GET | `/api/accounts` | Accounts |
| POST | `/api/accounts` | Account Create |
| POST | `/api/accounts/{aid}/toggle` | Account Toggle |
| POST | `/api/ai/draft/{mid}` | Draft |
| POST | `/api/ai/research/{cid}` | Research |
| GET | `/api/billing` | Billing Status |
| POST | `/api/billing/checkout/{tier}` | Checkout |
| POST | `/api/billing/portal` | Portal |
| POST | `/api/billing/webhook` | Billing Webhook |
| GET | `/api/booking` | Booking Availability |
| POST | `/api/booking` | Book |
| GET | `/api/calendar/{mid}.ics` | Calendar |
| GET | `/api/campaigns` | Campaigns |
| POST | `/api/campaigns` | Create Campaign |
| PUT | `/api/campaigns/{cid}` | Update Campaign |
| POST | `/api/campaigns/{cid}/{action}` | Campaign Action |
| GET | `/api/connections` | Connections |
| PUT | `/api/connections/{provider}` | Connection |
| GET | `/api/contacts` | Contacts |
| POST | `/api/contacts/import/{cid}` | Import Contacts |
| GET | `/api/contacts/{cid}/booking-link` | Get Booking Link |
| POST | `/api/contacts/{cid}/suppress` | Suppress Contact |
| GET | `/api/conversations` | Conversations |
| POST | `/api/demo` | Demo |
| POST | `/api/engine/tick` | Simulation Tick |
| GET | `/api/export/contacts` | Export Contacts |
| GET | `/api/health` | Health |
| GET | `/api/knowledge` | Knowledge List |
| POST | `/api/knowledge` | Knowledge Create |
| POST | `/api/knowledge/{nid}/{action}` | Knowledge Action |
| POST | `/api/login` | Login |
| POST | `/api/logout` | Logout |
| GET | `/api/materials/{cid}/{format}` | Materials |
| GET | `/api/me` | Me |
| GET | `/api/meetings` | Meetings |
| POST | `/api/meetings` | Meeting Create |
| POST | `/api/meetings/{mid}/{action}` | Meeting Action |
| POST | `/api/messages/{mid}/dismiss` | Dismiss |
| POST | `/api/messages/{mid}/reconcile` | Reconcile |
| POST | `/api/messages/{mid}/reply` | Reply |
| GET | `/api/metrics` | Metrics |
| POST | `/api/register` | Register |
| GET | `/api/sources/apollo` | Apollo Status |
| POST | `/api/sources/apollo/import/{cid}` | Apollo Import |
| POST | `/api/sources/apollo/search/{cid}` | Apollo Search |
| POST | `/api/unsubscribe` | Unsubscribe |
| GET | `/api/unsubscribe` | Unsubscribe |
| PUT | `/api/workspace` | Workspace Update |
| GET | `/book` | Booking Page |
