# Audit: LSP initialize timeout and reader robustness

Fecha: 2025-08-27

Resumen
-------
Durante la ejecución del script `audit/automated/run_audit.py` el proceso fallaba en la fase de `initialize` con un timeout: el script enviaba la petición `initialize` por stdin al LSP, pero no registraba la respuesta dentro del timeout configurado. El servidor sin embargo estaba vivo y emitiendo logs por stderr.

Hallazgos
---------
- El binario del Steel Language Server responde correctamente a `initialize` por stdin cuando se le prueba manualmente (se recibió un `result` válido).
- El script original leía stdout del proceso con llamadas bloqueantes simples que, en determinadas condiciones de scheduling o buffering, no ensamblaban correctamente los datos LSP (cabeceras + cuerpo) antes de agotar el timeout.
- En consecuencia, `incoming.jsonl` no se creaba y `run.log` registraba "Initialize failed: Timeout waiting for response 1".

Cambios aplicados
-----------------
1. Mejora del lector de stdout (`_reader`):
   - Sustituida la lectura bloqueante plana por un bucle que utiliza `select.select` para esperar por datos en el descriptor de archivo del stdout y `os.read` cuando es posible. Esto reduce probabilidades de bloqueo y fragmentación incompleta de mensajes.
   - Añadida lógica fallback por si select no funciona con el objeto de archivo.
   - Ensamblado del buffer y parseo robusto de mensajes LSP conforme al protocolo (Content-Length header + JSON payload).
2. Startup delay breve: se añadió un `time.sleep(0.05)` tras iniciar el hilo lector para reducir una condición de carrera en la que el proceso iniciaba y enviaba respuesta antes de que el lector quedase pronto a leer.
3. Logging adicional: trazas en `audit/automated/logs/run.log` indicando arranque del lector, bytes leídos y mensajes parseados, para facilitar diagnósticos futuros.

Resultado
---------
Tras aplicar los cambios el script ejecuta correctamente y completa la prueba automatizada. Ejemplo de salida relevante:

- Initialize response parseada y registrada.
- Notificaciones `textDocument/publishDiagnostics` detectadas para `macros/infix.stl` y `ffi/main.stl`.
- El script concluyó con `=== AUDIT RESULT: PASS ===`.

Recomendaciones
---------------
- Mantener el uso de `select` y el fallback; en entornos muy distintos (Windows vs Linux, distintos Python builds) los objetos de archivo pueden comportarse diferente.
- Reducir la verbosidad de los prints a stdout (usarlos solo bajo flag VERBOSE) y llevar la trazabilidad a `run.log` para CI limpio.
- Abrir un PR con estos cambios y añadir tests que verifiquen que `run_audit.py` produce un `incoming.jsonl` con la respuesta `initialize` y que no falla por timeout en un runner reproducible.

Acciones pendientes
-------------------
- Limpiar prints en `run_audit.py` (hecho en esta iteración si se solicita).
- Crear PR con el cambio, agregar reviewers.

Archivos modificados
--------------------
- audit/automated/run_audit.py (reader improved, startup delay, extra logging)
- audit/automated/logs/* (archivos generados durante la ejecución)

Autor: NeiBot (asistencia automatizada)