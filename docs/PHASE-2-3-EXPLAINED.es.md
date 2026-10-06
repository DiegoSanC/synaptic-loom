# Fases 2 y 3 explicadas — guía con ejemplos

Explicación detallada de cada requisito (R) de `PHASE-2-3-SPEC.md`, con ejemplos
paso a paso. Documento de apoyo en español; la spec normativa es la inglesa.

---

## 0. Mini-glosario (los términos que se usan en todo el documento)

- **Transcript**: el fichero JSONL donde Claude Code guarda, línea a línea, todo lo que
  pasa en una sesión (tus mensajes, sus respuestas, las herramientas que usó). Vive en
  `~/.claude/projects/<carpeta-codificada>/<id-de-sesión>.jsonl`.
- **Hook SessionEnd**: un script que Claude Code ejecuta automáticamente cuando una
  sesión termina. El nuestro hace dos cosas en <100 ms: escribe un *marcador* y lanza el
  *procesador* ("kick").
- **Marcador (marker)**: un JSON pequeño en `~/.loom/queue/` que significa "esta sesión
  tiene contenido pendiente de revisar". Es la lista de tareas del procesador.
- **Procesador**: el script (`loom process`) que consume marcadores: decide si la sesión
  pertenece a un proyecto registrado (el *gate*), extrae lo nuevo del transcript, llama
  al *summarizer*, escanea secretos y sube la entrada al servidor. No es un demonio: se
  ejecuta y muere.
- **Puntero (pointer)**: apunte local (`~/.loom/state.json`) de hasta qué línea de cada
  transcript ya se procesó. Es lo que evita resumir dos veces lo mismo cuando reanudas
  una sesión con `claude -r`.
- **Summarizer**: una sesión fresca de `claude -p` (modelo Sonnet, sin herramientas, sin
  acceso a disco ni red) que recibe el texto nuevo de la conversación y devuelve un
  resumen con las decisiones tomadas y su porqué — o la palabra `NO_ENTRY` si no hubo
  ninguna decisión que registrar.
- **Entrada (entry)**: el resumen de una sesión ya subido al servidor. Desde D7 cada
  entrada es un "concepto OKF": markdown con una cabecera YAML (frontmatter) que lleva
  su procedencia (sesión, rango cubierto, modelo, fecha, estado).
- **Logbook**: el conjunto de entradas de un proyecto. Lo que consulta el revisor con
  `/loom <proyecto>`.
- **Gate del piloto**: la evaluación de ~3 semanas (hasta ~14-08-2026) en la que el
  revisor anota si el logbook le ahorró tiempo. Su veredicto decide si la fase 3 se
  construye y con qué alcance.
- **Cuarentena (quarantine)**: carpeta local donde va una entrada si el escaneo detecta
  un posible secreto. No se sube nada; se revisa a mano con `loom push-quarantine`.

---

## FASE 2 — Robustez (no depende del gate)

### R2.1 — Barrido de huérfanos (orphan sweep)

**El problema.** El hook SessionEnd solo dispara en cierres limpios (`/exit`, fin de un
`claude -p`…). Si la sesión muere a lo bruto — cierras la ventana del terminal, se cae
WSL, un `kill -9` — no hay hook, no hay marcador, y esa sesión **se pierde para siempre**
aunque su transcript esté intacto en disco. En fase 1 lo aceptamos como pérdida asumida.

**La idea.** El transcript ES la fuente de verdad y sigue en disco. Un "barrido"
(sweep) periódico compara lo que hay en disco con lo que el puntero dice que ya se
procesó; si encuentra un transcript con contenido sin procesar y sin marcador, le crea
un **marcador sintético** (marcado con `"origin": "sweep"` para distinguirlo en los
logs) y el pipeline normal hace el resto.

**Ejemplo paso a paso.**
1. Lunes 10:00 — trabajas en el proyecto B (registrado), tomas la decisión "usamos
   Redis para la cache de sesiones".
2. Lunes 10:40 — Windows se reinicia por un update y se lleva WSL por delante. No hubo
   SessionEnd → no hay marcador. Hoy, esa decisión no llegaría nunca al logbook.
3. Martes 09:00 — enciendes el equipo. El barrido corre (lo lanza el timer de R2.2),
   recorre `~/.claude/projects/`, y encuentra el transcript del lunes: su última
   modificación fue hace >30 min (el umbral de "asentamiento" — evita procesar sesiones
   aún abiertas) y el puntero dice "procesado hasta la línea 0".
4. El barrido lee el campo `cwd` **de dentro del transcript** (cada línea lo lleva) —
   no se fía del nombre de la carpeta codificada — y crea el marcador sintético.
5. El procesador hace su pipeline normal: gate (¿B está registrado? sí) → delta →
   summarizer → escaneo → push. La decisión de Redis aparece en el logbook con un día
   de retraso, pero aparece.
6. Si el barrido vuelve a correr, no pasa nada: el puntero ya avanzó (idempotencia).

**Criterio de aceptación en cristiano:** mata una sesión sin piedad → su entrada acaba
en el servidor igualmente, y repetir el barrido no crea duplicados.

**¿Distingue una sesión abierta de una cerrada?** Con certeza, no — usa una
heurística: una sesión activa escribe constantemente en su transcript (cada mensaje
actualiza el mtime), así que "30 min sin escrituras" ≈ "no hay sesión detrás". El falso
positivo (sesión abierta pero ociosa >30 min, p. ej. te fuiste a comer) es inofensivo
por diseño: se resume el contenido parcial, el puntero captura el resto al cierre real,
`NO_ENTRY` filtra los parciales sin decisiones, y el peor caso es una conversación
partida en dos entradas — nunca duplicados ni pérdida. Señales más fuertes (`lsof`
sobre el fichero, un centinela de SessionStart) se consideraron y descartaron en v1:
acoplarse a la plataforma para eliminar un fallo que ya es barato.

**Pregunta abierta relacionada (Q-2.2):** lo que queda abierto es solo el VALOR del
umbral (¿30 min?) y si el primer barrido debe "rescatar" también sesiones antiguas de
antes de que existiera el barrido.

---

### R2.2 — Disparo periódico (timer)

**El problema.** Hoy el procesador solo corre cuando (a) cierras una sesión (kick) o
(b) lo lanzas a mano. Consecuencia: un marcador que quedó pendiente (p. ej. el servidor
estaba caído justo al cerrar) espera **hasta tu siguiente cierre de sesión**, que puede
ser al día siguiente. Y el barrido de R2.1 necesita alguien que lo ejecute regularmente.

**La idea.** Un temporizador del sistema ejecuta `loom process` cada 15 minutos (que a
su vez incluye el barrido). En máquinas con systemd de usuario se usa un *systemd user
timer* (la versión moderna de cron: se declara "cada 15 min ejecuta esto" y systemd se
encarga); donde no, un *cron* clásico (el planificador de tareas de Unix). El installer
lo deja puesto y `loom doctor` comprueba que está activo.

**Ejemplo paso a paso.**
1. Viernes 18:00 — cierras la última sesión. El kick corre, pero el servidor está
   caído (reinicio de la VM). El marcador se conserva (matriz de errores: fallo de red
   = reintento infinito barato, sin consumir intentos).
2. Sin timer (hoy): ese marcador esperaría hasta tu próximo cierre de sesión — quizá el
   lunes.
3. Con timer: a las 18:15 el timer lanza `loom process`, el servidor ya volvió, la
   entrada sube. Nadie hizo nada.

**Matiz WSL2** (por qué esto no estaba en fase 1): en WSL el "ordenador" (la VM) se apaga
cuando cierras todas las terminales, y un timer apagado no dispara. Por eso el piloto usó
el kick (P1). El timer es un *añadido* al kick, no un sustituto — cubre los huecos cuando
la VM está encendida, y sus limitaciones quedan documentadas en el RUNBOOK.

**Criterio de aceptación:** con el servidor caído, cierra una sesión; levanta el
servidor; la entrada aparece sola en ≤15 min.

---

### R2.3 — Escaneo de secretos completo (gitleaks)

**El problema.** Antes de subir nada, el procesador escanea el resumen buscando
secretos (claves API, contraseñas…). Hoy usa un conjunto de expresiones regulares
propias que reconocen formatos *conocidos*: `AKIA…` (AWS), `sk-…` (OpenAI), `ghp_…`
(GitHub), bloques PEM, `password=`… Su punto ciego: secretos **sin formato reconocible**
— un token interno de tu organización tipo `x7Kp2mQv9…` no casa con ninguna regex.

**La idea.** Integrar **gitleaks**, el escáner open source estándar del sector, que
además de cientos de reglas usa **detección por entropía**: mide cuán "aleatoria" es una
cadena (un token criptográfico tiene una aleatoriedad estadística que las palabras
normales no tienen) y marca las sospechosas. Las regex propias se quedan como respaldo
si gitleaks no está instalado, y `loom doctor` te dice qué motor está activo.

**Ejemplo paso a paso.**
1. Depurando el proyecto A pegas en la conversación un token interno:
   `INTERNAL_TOKEN=x7Kp2mQv9rT4wY8zB3nF6jH1`.
2. El summarizer, pese a la regla del prompt ("nunca incluyas credenciales"), lo
   reproduce en el resumen — los LLM fallan a veces; por eso el escaneo determinista
   existe (defensa en profundidad, invariante I4).
3. Hoy: las regex no reconocen ese formato → el token **se sube al servidor**. Fuga.
4. Con R2.3: gitleaks lo detecta por entropía → la entrada va a cuarentena → tú la
   revisas con `loom push-quarantine`, borras el token, subes la versión limpia.
5. Fail-closed intacto: si gitleaks casca (bug, binario roto), se trata como si hubiera
   encontrado algo — a cuarentena. Nunca "no pude escanear, lo subo igual".

**Criterio de aceptación:** un token de alta entropía que las regex no pillan acaba en
cuarentena cuando gitleaks está activo.

---

### R2.4 — Pulido del installer

**El problema.** Dos operaciones cotidianas son hoy manuales: instalar el timer de R2.2
no existe, y **actualizar** el cliente en una máquina requiere saber qué re-copiar (el
código va solo por el install editable, pero el hook, el prompt y las skills son copias).

**La idea.** `install.sh` instala también el timer, y gana un modo `--update`:
`git pull` + re-copiar hook/prompt/skills + `loom doctor` de verificación. Un comando.
`loom doctor` aprende tres checks nuevos: ¿timer activo?, ¿cuándo corrió el último
barrido?, ¿qué motor de escaneo hay y en qué versión?

**Ejemplo.** Publicamos una mejora del prompt del summarizer. Hoy: cada dev tiene que
acordarse de `git pull` **y** de copiar el prompt a `~/.loom/prompts/` (si no, seguirá
usando el viejo sin saberlo). Con R2.4: `bash client/install.sh --update` y doctor en
verde. Fin.

**Criterio de aceptación:** instalación desde cero y actualización en sitio dejan
`loom doctor` completamente verde.

---

## FASE 3 — Equipo completo (condicionada al gate)

### R3.1 — Exposición del servidor (TLS / salir de localhost)

**El problema.** El servidor del piloto escucha solo en `127.0.0.1` — el "localhost" de
TU máquina. Dev2 y Dev3 literalmente no pueden conectarse. Y en cuanto el servidor sea
alcanzable por red, el tráfico lleva tokens y contenido de conversaciones → **TLS
obligatorio** (el cifrado de HTTPS; la política de DESIGN §8 prohíbe HTTP plano fuera de
localhost).

**Las dos opciones (decisión pendiente, Q-3.1):**
- **VM interna + Caddy**: el servidor se muda a una VM propia; delante se pone
  Caddy (un servidor web que hace de *reverse proxy*: recibe el HTTPS, gestiona el
  certificado, y reenvía al uvicorn interno).
- **Tailscale serve**: si el equipo usa Tailscale (una VPN de malla), él mismo expone el
  servicio cifrado dentro de la red privada, sin certificados que gestionar.

Incluye la **migración** desde tu WSL: parar → copiar `loom.db` y backups → arrancar en
la VM → cada cliente repite `loom init` con la URL nueva. Si además se rotan los tokens
en la mudanza es Q-3.2.

**Ejemplo.** Dev3 tiene que revisar tu proyecto. Hoy: `loom log` desde su máquina →
connection refused. Con R3.1: su config apunta a `https://loom.example.internal`, el
`loom log` viaja cifrado, y tu entrada del lunes le aparece en su terminal.

**Criterio de aceptación:** `loom log` funciona desde una segunda máquina vía TLS y los
datos del piloto sobreviven la migración.

---

### R3.2 — UI web mínima

**El problema.** Todo pasa hoy por el CLI. Vale para nosotros; no vale para navegar
cómodamente el histórico, ni para dar de alta usuarios sin tocar `admin.py` en el
servidor, ni para el futuro compañero que solo quiere *leer*.

**El alcance** (deliberadamente mínimo): login → lista de proyectos → logbook navegable
por proyecto/fecha/autor → registrar un proyecto a mano (misma comprobación de
duplicados que el CLI: si ya estaba, te avisa de quién y cuándo) → mergear rutas
sin-remote (ver R3.3) → gestión de tokens (crear/revocar).

**Los tres huecos de definición que tiene delante** (por eso la spec la manda a revisión
antes de construir):
- **Q-3.3 — ¿login con qué?** El sistema solo conoce *bearer tokens* (una cadena secreta
  que el CLI manda en cada petición: `Authorization: Bearer <token>`). No hay contraseñas
  ni sesiones. ¿La UI pide "pega tu token" (simple, feo), o montamos sesiones/SSO (caro)?
  *Candidato híbrido (2026-07-29):* el login de la UI valida tu token opaco y emite una
  **sesión firmada de vida corta** para el navegador (cookie firmada, o JWT corto — con
  TTL de minutos/horas su no-revocabilidad deja de importar). El CLI/procesador no se
  toca. **JWT como credencial del API quedó descartado** (análisis en Q-3.2 de la spec):
  no se puede revocar al instante sin reintroducir estado en el servidor, y su
  expiración rompería al procesador desatendido (push nocturno con 401 hasta que un
  humano "re-loguee").
- **Q-3.4 — ¿quién es admin?** Revocar el token de otro o mergear rutas no debería
  poder hacerlo cualquiera… pero el esquema de la DB no tiene roles.
- **R3.2.3 — construir DESPUÉS del visualizador OKF (R3.5):** si el visualizador ya
  resuelve el "navegar el logbook", la UI encoge a login+registro+merge+tokens.

**Ejemplo.** Se incorpora un cuarto dev. Sin UI: tú entras por SSH al servidor, corres
`admin.py adduser`, le pasas el token por un canal seguro, y él configura el CLI. Con
UI: creas su usuario en dos clics, y él navega el logbook de su proyecto heredado desde
el navegador antes siquiera de instalar el cliente.

---

### R3.3 — Registro por ruta + merge (repos sin remote)

**El problema.** La identidad de un proyecto es su **git remote** normalizado
(`github.com/org/b`), lo que hace que el mismo repo clonado en tres máquinas sea UN
proyecto sin configurar nada. Pero un repo **sin remote** (prototipo local que nunca se
subió) no tiene esa identidad: hoy `loom register` te dice "no hay remote" y sale con
error 1. Sus sesiones no se capturan.

**La idea.** El plan B diseñado en fase 1 y aplazado: registrar por **ruta** (la tabla
`project_paths` ya existe en el esquema). Como la ruta es distinta en cada máquina
(`/home/ana/protos/x` vs `/home/dev3/experiments/x`), la UI ofrece el **merge**:
"estas dos rutas son el mismo proyecto lógico". El gate del procesador aprende a
consultar rutas registradas cuando no resuelve un remote.

**Ejemplo paso a paso.**
1. Prototipas `~/protos/motor-reglas` sin remote. `loom register` → te ofrece
   registrarlo por ruta como proyecto "motor-reglas". Tus sesiones ahí empiezan a
   capturarse.
2. Dev3 recibe una copia y la pone en `~/experiments/motor-reglas`. Registra su ruta.
   Ahora mismo el servidor ve DOS proyectos.
3. En la UI: merge de ambas rutas → un solo proyecto lógico. Las entradas de los dos
   quedan en el mismo logbook, y `/loom motor-reglas` desde cualquiera de las dos
   máquinas lo ve todo.

**Nota importante:** es el *fallback*, no el mecanismo principal. En cuanto el repo
tenga remote, la identidad buena es la del remote.

---

### R3.4 — Digest de curación (obsolescencia del logbook)

**El problema — el más sutil de la fase 3.** El logbook es *append-only* (solo se
añade, nunca se borra ni se modifica — a propósito: es un registro histórico con
`content_hash` único). Pero las decisiones caducan: en marzo decidiste "SQLite"; en
junio, "migramos a Postgres". Las dos entradas conviven, y un revisor que lea la de
marzo puede creer vigente algo que ya no lo es. Con meses de acumulación, el logbook
pierde justo lo que lo hace valioso: la confianza.

**La idea.** Un pase periódico de **curación**: otro `claude -p` con el mismo perfil de
seguridad que el summarizer (sin herramientas, entrada por stdin, escaneo fail-closed)
lee las entradas recientes de un proyecto y produce un **digest**: "estado actual de
las decisiones", marcando cuáles quedaron **superseded** (sustituidas) y por cuál.

**El gap de definición (Q-3.5), con las tres opciones sobre la mesa:** el estado
`status: current` vive dentro del frontmatter de un `content_md` inmutable. Marcar
"superseded" exige elegir mecanismo:
1. Columna `status` mutable + endpoint PATCH → simple, pero rompe el contrato
   append-only.
2. Tabla de enlaces de supersesión (`entry 12 superseded_by entry 47`) → preserva la
   inmutabilidad; las entradas no cambian, la relación vive aparte.
3. Solo-digest: el digest es una entrada más que declara el estado; nada se marca →
   máxima pureza, pero los consumidores tienen que leer el digest para saber qué vale.

**Ejemplo paso a paso.**
1. Marzo, entrada #12: "**SQLite como almacén** — sin concurrencia esperada. Status:
   current".
2. Junio, entrada #47: "**Migramos a Postgres** — 3 máquinas escribiendo. Status:
   current".
3. El pase de curación corre (p. ej. semanal), detecta que #47 revierte #12, y produce
   el digest: "Almacén: **Postgres** (#47). Nota: #12 (SQLite) superseded por #47".
4. Dev3 revisa un cambio de persistencia: `/loom` le enseña el estado curado primero —
   imposible creerse la decisión de marzo.

**Criterio de aceptación:** tras una sesión que revierte una decisión anterior, el
digest la marca superseded y enlaza la sustituta; `loom log` distingue ambas.

---

### R3.5 — Export OKF + visualizador estático

**El problema/oportunidad.** Desde D7 cada entrada ya es un concepto **OKF** (Open
Knowledge Format, el formato abierto de Google: ficheros markdown con cabecera YAML que
cualquier agente puede ingerir directamente, sin SDK). Pero hoy viven como filas en la
base de datos, accesibles solo vía nuestro API JSON. El que quiera consumirlas necesita
nuestro CLI.

**La idea.** Dos piezas:
- **`GET /api/projects/{id}/okf`** devuelve el **bundle** del proyecto: un directorio
  (¿zip? ¿rutas por fichero? — Q-3.6) con un `.md` por entrada más un `index.md`
  generado. Gracias a D7 esto es casi literal: escribir los `content_md` a ficheros.
- **El visualizador estático de Google** (un HTML autocontenido, sin backend, que pinta
  el bundle como grafo navegable con búsqueda y filtros) servido sobre ese bundle → un
  frontend de *solo lectura* gratis. Es la pieza que puede encoger la UI de R3.2.
- Misma autenticación que el API (un bundle contiene todo el conocimiento del proyecto).

**Ejemplo 1 — humano:** Dev2 quiere ojear la historia del proyecto C antes de heredarlo.
Abre el visualizador → grafo de decisiones por fecha, filtra por `tags: ["c"]`, clica
una decisión y lee su evidencia. Cero CLI.

**Ejemplo 2 — agente (la gracia del formato):** el equipo de datos tiene su propio
agente Gemini. Sin saber nada de Loom: descarga el bundle, `cat` de los ficheros, y su
LLM ya tiene las decisiones con procedencia. El formato ES el contrato — no hay que
integrarse con nuestro API.

**Criterio de aceptación:** un agente sin tooling de Loom ingiere el bundle; el
visualizador pinta el grafo; sin token no hay bundle.

---

### R3.6 — Onboarding del equipo

**El problema.** Todo lo anterior es infraestructura; el objetivo real del proyecto es
que el equipo, que se revisa los proyectos entre sí, la use de verdad.

**La idea.** Incorporar al resto del equipo: usuario + token para cada uno, install del
cliente en sus máquinas (`install.sh` + `loom init` + `loom doctor`), y que cada uno
registre ≥1 proyecto real — respetando la política de seguridad: **nada que procese
datos reales de cliente** sin sign-off de compliance (DESIGN §8). El RUNBOOK se
reescribe de "piloto de una máquina" a "despliegue de equipo".

**Ejemplo — el flujo completo por fin de punta a punta:**
1. Dev2 trabaja el martes en el proyecto B: "elegimos idempotencia por content-hash en
   el webhook porque el proveedor reenvía duplicados". Cierra sesión → entrada al
   logbook, sola.
2. El jueves te toca revisar su PR del webhook. En tu sesión de revisión: `/loom
   project-b` → ves la decisión y el porqué **antes de abrir el diff**.
3. La pregunta que ibas a hacerle por Slack ("¿por qué content-hash y no el ID del
   proveedor?") ya está respondida en la evidencia de la entrada. Revisas en 20 min lo
   que antes eran 45.

Ese punto 3 es la tesis completa del proyecto. Todo lo demás existe para que ese
momento ocurra de forma fiable.

---

## Cómo se relacionan (dependencias en una frase cada una)

- **R2.2 ejecuta a R2.1**: el timer es quien lanza el barrido regularmente.
- **R2.4 instala a R2.2**: el installer deja el timer puesto.
- **R3.2 espera a R3.5**: primero el visualizador; la UI cubre solo lo que él no puede
  (login, registro, merge, tokens).
- **R3.5 se apoya en D7 (fase 1)**: el export es trivial porque las entradas ya son OKF.
- **R3.4 necesita resolver Q-3.5 antes de diseñarse**: sin mecanismo de supersesión no
  hay curación.
- **R3.6 necesita R3.1**: sin servidor expuesto no hay equipo que conectar.
- **Todo R3.x está condicionado al gate** (spec §6); todo R2.x va ya.
