# Trampas de evaluación

Cada una apareció al postular de verdad con el radar. El síntoma es lo que el motor decía;
la causa, por qué estaba mal; la regla, lo que hace ahora. Casi todas tienen su prueba en
`tests/test_motor.py` o `tests/test_demo.py`.

| # | Síntoma | Causa | Regla |
|---|---|---|---|
| 1 | Un programa de entrada de un banco global salía "cumple parcialmente" | "with no prior work experience" se trataba como una brecha más | Es elegibilidad: "cumple poco", −60 puntos, siguiente paso "No postular". "Con o sin experiencia" no dispara la regla |
| 2 | Una consultora decía "pide 4+ años; el perfil declara 3" | "Up to 4 years" se leía como mínimo | "up to / maximum / hasta / máximo N" es un tope. Superarlo sí es brecha |
| 3 | Un cargo de organismo internacional salía "cumple completamente" | "An advanced university degree in finance…" no decía *required* | Una línea que empieza con el título exigido es requisito; con *preferred* u *or equivalent* no |
| 4 | Ese mismo cargo salía "cumple completamente" sin requisitos | El listado solo decía "T6. Closes Oct 11" y cero brechas se leía como encaje | Sin requisitos publicados, "sin descripción para comparar" |
| 5 | Un cargo con funciones extensas salía "cumple completamente" | "clearance requirements" en las funciones se tomaba como encabezado de requisitos | En fuentes de solo resumen se exige un encabezado real al inicio de una línea corta |
| 6 | Una firma de consultoría salía "cumple completamente" | "Certificación en Finanzas, Data Science…" no nombraba CFA ni CPA | Una línea que empieza con "Certificación / Certification" sin *deseable* es brecha |
| 7 | Cargos de inversión de un banco de desarrollo salían "parcial" | El núcleo del cargo era "track record of executing debt and equity investments" | Historial exigido de transacciones pesa 3: "cumple poco" |
| 8 | "Resident Advisor" y "Senior Economist" ocupaban el Top 20 | El filtro de nivel profesional no conocía *advisor* | Cargos profesionales de multilaterales sin marca de entrada pesan 3 |
| 9 | "2025 … Analyst Program" aparecía en octubre de 2026 | El portal no retira programas vencidos | Un año pasado en el título es convocatoria vencida |
| 10 | Avisos de hace 7 meses competían con los de esta semana | La antigüedad solo sumaba, nunca restaba | 91–180 días: −5; más de 180: −15 |
| 11 | Las vacantes ya postuladas seguían ocupando la lista | El radar no sabía qué se había enviado | `--seguimiento`: empresa igual y ≥60 % de palabras del cargo en común = ya en seguimiento |
| 12 | Un portal regional mostraba vacantes de otras empresas | La búsqueda pública caía a resultados genéricos | Se valida el empleador de cada aviso; si no coincide, la fuente queda vacía, no inventada |
| 13 | Vacantes "cerradas" que seguían abiertas | Una lista recortada (20 por empresa) se comparaba contra la anterior | Solo una fuente leída completa puede cerrar vacantes |
| 14 | Un aviso abierto salía "cerrado" | El texto oculto de traducciones decía "no longer accepting" | Se verifica con datos estructurados (JSON-LD, `canApply`) y solo texto visible |

## Lo que no se automatiza

- **Portales que bloquean lecturas automáticas** quedan como revisión manual. No se rodean.
- **LinkedIn** no se lee de ninguna forma.
- **Los requisitos que el listado no publica** se leen abriendo el aviso. El radar lo dice.
