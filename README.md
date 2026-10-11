# Radar de vacantes

[![ci](https://github.com/s-osorio-gom/radar-de-vacantes/actions/workflows/ci.yml/badge.svg)](https://github.com/s-osorio-gom/radar-de-vacantes/actions/workflows/ci.yml)

Lee todos los días los portales de empleo de las empresas que le interesan, compara cada
aviso con su perfil y le entrega **las 20 mejores vacantes, verificadas en el portal
original**. Para cada una dice si cumple los requisitos completa o parcialmente, cuáles
son las brechas reales y qué hacer después.

No es un buscador con filtros. El problema que resuelve es otro: los portales listan miles
de avisos, y los que parecen perfectos por el título a menudo no lo son. Hay programas que
exigen *no* tener experiencia, cargos que piden maestría sin decir "required", y
convocatorias de 2025 que siguen publicadas en 2026. El radar lee la letra pequeña.

```text
$ uv run radar demo
118  cumple completamente           Red de Pagos (ficticia) — Senior Analyst, Commercial Strategy & Monetization
       siguiente paso: Preparar postulación y enviar en el portal
108  cumple completamente           Consultora Estratégica (ficticia) — Associate, Colombia 2027
       siguiente paso: Preparar postulación y enviar en el portal
103  cumple parcialmente            Firma de Servicios (ficticia) — Consultor Senior de Estrategia
       brecha: pide 3+ años; el perfil declara 2
       brecha: pide certificación: Certificación en Finanzas, Data Science o Gestión de Proyectos.
       siguiente paso: Preparar postulación cerrando las brechas en la carta
 86  cumple parcialmente            Organismo Internacional (ficticio) — Junior Economist
       brecha: exige posgrado (maestría/MBA)
       siguiente paso: Revisar elegibilidad por nacionalidad en el aviso y preparar
 81  sin descripción para comparar  Organismo Internacional (ficticio) — Junior Economist – Capital Markets
       siguiente paso: Revisar elegibilidad por nacionalidad en el aviso y preparar
 75  cumple poco                    Brazo de Inversión (ficticio) — Associate Investment Officer
       brecha: pide historial ejecutando inversiones o transacciones; no lo tienes
       siguiente paso: Apuesta alta: postular solo si te interesa mucho el empleador
 27  cumple poco                    Banco Global (ficticio) — Graduate Program - Markets
       brecha: no elegible: exige no tener experiencia laboral previa y tú la tienes
       siguiente paso: No postular: no cumples un requisito de elegibilidad
 27  cumple poco                    Banco Global (ficticio) — 2025 Markets Analyst Program
       brecha: convocatoria restringida (programa 2025, ya pasado)
       siguiente paso: No postular: convocatoria restringida o vencida
```

<sub>Salida de `radar demo`: 8 vacantes sintéticas de [`ejemplo/vacantes.json`](ejemplo/vacantes.json) evaluadas con el perfil ficticio de [`config/perfil.ejemplo.yaml`](config/perfil.ejemplo.yaml).</sub>

## Cómo funciona

```text
config/universo.yaml ─► fuentes (sources.py) ─► normalización (boards.py) ─► detalle y deduplicación (engine.py)
                                                                                   │
salida/*.md, *.json ◄─ reporte (report.py) ◄─ verificación en el portal (verify.py) ◄─ evaluación contra su perfil (evaluate.py)
```

1. **Fuentes.** Cada empresa del universo declara cómo se leen sus vacantes (API de un ATS,
   Workday, Oracle…). Cada lectura termina en un resultado explícito; una respuesta con forma
   desconocida es `error`, nunca "sin vacantes".
2. **Normalización.** Todo aviso se convierte al mismo `JobPosting` (empresa, cargo, ciudad,
   fecha, salario, descripción). Lo que la fuente no trae queda vacío; no se inventa.
3. **Detalle y deduplicación.** Para los avisos prometedores se lee la descripción completa.
   Un mismo cargo en dos fuentes (empresa + cargo + ciudad) queda una vez, con el portal
   original por encima del agregador.
4. **Evaluación.** Reglas determinísticas contra `config/perfil.yaml` (ver *Cómo evalúa*).
5. **Verificación.** Las mejores se comprueban una por una en el portal original.
6. **Reporte.** Markdown y JSON en `salida/`, más un historial para saber qué es nuevo y qué
   se cerró.

## Lo que este repositorio impide hacer mal

Cada regla nació de un error real del motor, encontrado al postular de verdad. Todas
tienen su prueba en [`tests/test_motor.py`](tests/test_motor.py), y la lista completa
con síntoma, causa y regla (14 casos) está en [`docs/TRAMPAS.md`](docs/TRAMPAS.md):

1. **"Sin experiencia previa" es elegibilidad, no brecha.** Un programa de entrada que
   exige no haber trabajado no se "cierra en la carta": no procede.
2. **"Hasta 4 años" es un tope, no un mínimo.** Leerlo como "pide 4+ años" descarta la
   vacante perfecta.
3. **"An advanced university degree in…" es una exigencia** aunque no diga *required*.
4. **Sin requisitos publicados no hay "cumple completamente".** Varios portales de
   organismos internacionales listan solo el resumen del cargo. No encontrar brechas en un
   texto vacío no es encajar.
5. **Una convocatoria con un año pasado en el título está vencida**, aunque el portal la
   siga mostrando.
6. **Las vacantes a las que ya postuló no ocupan cupo** en la lista del día.

## Qué lee

Un universo de empresas en [`config/universo.yaml`](config/universo.yaml): 112 empresas de
ejemplo (banca global, multilaterales, consultoría, pagos, finanzas en Colombia) y 68
boards de ATS. Cada fuente devuelve un resultado explícito (`ok`, `empty`, `error`,
`blocked`, `login`, `not_found`, `manual`): un portal caído nunca se registra como "sin
vacantes".

| Sistema | Cómo se lee |
|---|---|
| Greenhouse, Ashby, Lever, SmartRecruiters, Workable, Recruitee | API pública de cada ATS |
| Workday | Búsqueda pública (`/wday/cxs/…/jobs`) y detalle con fecha de inicio y `canApply` |
| Oracle Recruiting Cloud | `recruitingCEJobRequisitions` con finder público |
| SAP SuccessFactors (Career Site Builder) | HTML público de búsqueda, paginado |
| Phenom | El mismo `POST /widgets` que hace el sitio, con detalle por vacante |
| Cornerstone OnDemand | Búsqueda pública con el token anónimo que entrega el propio sitio |
| Impactpool, HireHive, amazon.jobs, Magneto365 | JSON o JSON-LD públicos |
| Indeed | No se consulta. Opcionalmente se carga una transcripción JSON que usted obtenga (`--indeed archivo.json`) |
| Portales sin API legible | Se registra el acceso y se revisan a mano |

**Lo que no hace.** No inicia sesión, no llena formularios, no envía nada, no resuelve
CAPTCHA ni rodea bloqueos anti-bots. Si un portal bloquea las lecturas automáticas, queda
como "bloqueada" o "revisión manual". Nunca lee LinkedIn.

## Cómo evalúa

Todo es código determinístico, sin modelos de lenguaje: el mismo aviso con el mismo perfil
da siempre el mismo resultado, y cada punto se puede explicar.

- **Afinidad** por familias de cargo (finanzas corporativas, estrategia, pricing, FP&A,
  tesorería y riesgo…) y por "carreras de entrada" en consultoría y banca.
- **Requisitos** indispensables y deseables separados por encabezados y marcas en línea;
  años, posgrado, certificaciones, herramientas e inglés comparados con el perfil.
- **Autorización de trabajo** en seis clases (Colombia, remoto, patrocinio, organismo
  internacional, exige autorización, sin información). Nunca se presupone.
- **Antigüedad:** ≤15 días suma; más de 3 meses resta; "sin fecha" nunca se estima.
- **Salario** publicado comparado con su piso (`floor_monthly_cop`), con la TRM oficial
  vigente ([datos.gov.co](https://www.datos.gov.co/)) para avisos en USD. Sin TRM, los avisos
  en USD no se puntúan por salario. La moneda sale del código escrito (COP, pesos, USD); con
  "$" a secas, "$4.500.000" es COP y "$80,000" es USD. Otras monedas, o dos en el mismo
  texto, no se puntúan.
- **Inglés:** un aviso que pide inglés fluido o avanzado es brecha solo si su perfil declara
  menos de C1.
- **Sus gustos:** puntos por sector y familia (`preferences` del perfil). No borran
  vacantes, solo cambian el orden. También desde el perfil: en qué sectores "Analyst" o
  "Associate" a secas cuenta como carrera de entrada (`entry_track_sectors`).

Luego toma las mejores, máximo 3 por empresa, y **verifica cada una en el portal
original** (Workday `canApply`, JSON-LD `validThrough`, 404/410 = cerrada) antes de
recomendarla.

## Requisitos

- [uv](https://docs.astral.sh/uv/) (instala Python 3.13 si no lo tiene).
- Conexión a internet solo para `radar buscar` y `radar descubrir`. La demo y las pruebas no
  usan la red.

## Instalación

```bash
git clone https://github.com/s-osorio-gom/radar-de-vacantes.git
cd radar-de-vacantes
uv sync
uv run radar demo            # sin red: vacantes sintéticas con el perfil de ejemplo
```

También se puede instalar como comando (`uv tool install .` desde el clon); la demo funciona
igual, pero `radar buscar` debe correr en una carpeta que tenga `config/universo.yaml`.

## Su perfil

```bash
cp config/perfil.ejemplo.yaml config/perfil.yaml
# edítelo: años, inglés, título, herramientas, piso salarial, fortalezas y gustos
uv run radar demo --perfil config/perfil.yaml   # valida el perfil y lo prueba sin red
```

Si el perfil está mal formado (un número escrito como texto, una expresión regular inválida),
el radar se detiene y dice qué campo corregir. Ponga solo lo que pueda sostener en una
entrevista: una brecha honesta vale más que un falso "cumple". `config/perfil.yaml` no se
versiona (está en `.gitignore`), igual que `salida/` y su seguimiento de postulaciones.

Luego revise `config/universo.yaml`: es el universo de ejemplo de este repositorio. Quite las
empresas que no le interesen y agregue las suyas (`radar descubrir`, abajo).

## Uso

```bash
uv run radar buscar          # corrida completa: varios minutos según la red
```

`radar buscar` escribe en `salida/`:

- `Top20_<fecha>.md`: la lista verificada, con requisitos, brechas y siguiente paso.
- `Busqueda_<fecha>.md`: todas las vacantes por prioridad, novedades y cobertura por fuente.
- `busqueda_latest.json` y `tablero_vacantes.json`: lo mismo para un tablero o una hoja.
- `historial/`: un registro de cada corrida que no se borra. De ahí salen las vacantes
  nuevas y cerradas.

Opciones útiles: `--seguimiento postulaciones.md` (una tabla Markdown con columnas
Empresa | Puesto | Estado; esas vacantes no ocupan cupo), `--indeed resultados.json`,
`--top 30`, `--zona America/Mexico_City`. Un archivo que no existe o una zona inválida se
reportan antes de leer cualquier portal.

Para agregar una empresa, `radar descubrir "Empresa" https://example.com/careers` le dice
qué sistema de vacantes usa y con qué referencia ponerla en `universo.yaml`.

## Corrida diaria

[`scripts/`](scripts/) trae un `launchd` para macOS (reemplace `RUTA_DEL_REPO` en el
`.plist`) y una línea de `cron` para Linux que corren el radar a las 4:00 a. m. El
historial hace que cada mañana vea qué es nuevo y qué se cerró.

## Uso responsable y límites

El radar lee solo información que los portales publican sin iniciar sesión, al ritmo de una
persona que revisa varias páginas, y se identifica como lo que es:

- **User-Agent propio** (`radar-de-vacantes/1.0; solo lectura`), sin cookies ni credenciales.
- **robots.txt:** antes de leer un sitio consulta su `robots.txt` y no lee las rutas que
  prohíbe; esa fuente queda como `blocked` con el motivo. Si el `robots.txt` no responde
  (error 5xx o de red), el sitio no se lee en esa corrida.
- **Ritmo:** al menos medio segundo entre pedidos al mismo host, tiempo de espera de 20 s,
  respuestas de 20 MB como máximo y dos reintentos con espera solo ante fallos pasajeros.
  Un 403, un 429 o un CAPTCHA no se reintentan.
- **Solo `http(s)`:** las URLs que vienen dentro de los avisos son datos externos; nunca se
  abre `file://` ni otro esquema, y los reportes escapan el HTML y los enlaces.

Lo que el código no puede garantizar por usted:

- **Términos de uso.** Que un dato sea público no significa que su lectura automatizada
  esté permitida. Algunos portales lo prohíben en sus términos aunque su `robots.txt` no lo
  diga. Antes de agregar una empresa a `universo.yaml`, revise los términos del sitio y
  quítela si no lo permiten.
- **Endpoints internos.** Workday, Oracle, Phenom, Cornerstone y amazon.jobs se leen con
  los mismos pedidos que hace su página pública (en Cornerstone, con el token anónimo que el
  sitio entrega a todo visitante); SuccessFactors, Impactpool y Magneto365, leyendo su HTML.
  No son APIs documentadas para terceros: pueden cambiar o cerrarse sin aviso, y su uso queda
  bajo su responsabilidad.
- **Datos personales.** Los avisos pueden traer nombres o correos de reclutadores. El
  radar los guarda solo en `salida/`, en su equipo. No los publique ni los reutilice.
- **Uso personal.** Está pensado para la búsqueda de una persona, una vez al día. No lo use
  para republicar vacantes, revenderlas ni para barridos masivos.

Este repositorio no es asesoría legal.

## Limitaciones

- **Hecho para una búsqueda en Colombia.** La autorización de trabajo distingue "Colombia",
  "remoto desde Colombia" e internacional, y el piso salarial está en COP. Para otro país hay
  que ajustar `_CO` y `AUTH_POINTS` en `evaluate.py`.
- **Hecho para finanzas y estrategia.** Las familias de cargo (`_FAMILIES` en `evaluate.py`)
  reconocen finanzas corporativas, FP&A, pricing, estrategia, economía, tesorería y datos.
  Para otra carrera hay que editarlas; el perfil no basta.
- **Portales leídos como HTML** (SuccessFactors, Impactpool): si cambian su diseño, la fuente
  puede aparecer vacía en lugar de con error, porque una página realmente vacía se ve igual.
- **Endpoints no documentados:** pueden cambiar sin aviso. Las pruebas usan respuestas de
  ejemplo, no los portales reales; la primera señal de un cambio es un `error` con
  "formato inesperado" en la cobertura.
- **Lo que no publica el aviso no se evalúa:** sin requisitos publicados, el resultado es "sin
  descripción para comparar", nunca "cumple".

## Verificación

```bash
uv run pytest        # sin red, con un cliente HTTP falso
uv run ruff check .
uv run pyright       # tipos
uv run radar demo
```

Es lo mismo que corre [`ci.yml`](.github/workflows/ci.yml). Las pruebas no tocan internet:
cada conector se prueba contra respuestas de ejemplo, cada trampa de evaluación contra un
texto como el que la produjo, y el cliente HTTP real se prueba sin abrir conexiones
(robots.txt, ritmo por host, esquemas de URL). [`tests/test_fallos_red.py`](tests/test_fallos_red.py)
simula timeout, 429, 5xx, JSON malformado, respuestas con forma inesperada y un conector que
falla: ninguno detiene la corrida ni cierra vacantes en el historial.

## Licencia

MIT. Úselo, cámbielo y adáptelo a su búsqueda.
