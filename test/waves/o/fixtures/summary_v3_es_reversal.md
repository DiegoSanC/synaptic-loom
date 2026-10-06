## Context
Se decidió cómo cachear las respuestas de `/api/stats`, que actualmente tardan unos 2 segundos.

## Decisions
- [decision] **Cachear en una tabla `stats_cache` en SQLite con TTL de 60 s, en `server/stats.py`** — se descartó la caché en memoria del proceso porque en Fly puede haber más de una máquina y cada una tendría su propia caché. Evidence: "Mejor no en memoria: en Fly podemos tener más de una máquina y cada una tendría su propia caché."
- [task] **Añadir índice por `project_id` a `stats_cache`** — asignado a Marta, la semana que viene, en otro PR. Evidence: "El índice por project_id lo dejamos para otro PR; que lo haga Marta la semana que viene."

## Open questions
- ¿Se invalida la caché al crear una entrada nueva, o se espera a que caduque el TTL de 60 s?
