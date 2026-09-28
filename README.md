# Hermes PM V1

Panel de investigación de wallets públicas de Polymarket y simulador forward-only de compras. No contiene autenticación de Polymarket, llaves de wallet ni rutas de órdenes reales.

## Inicio

```bash
HERMES_PM_KEY='una-clave-larga' START_CASH=1000 python app.py
```

Abrir `http://localhost:8000`. Ingresar la clave en el panel. `GET /health` es público; el resto de la API requiere cabecera `X-Hermes-Key`.

## Despliegue Railway

Crear servicio desde este repositorio, Dockerfile detectado automáticamente; configurar `HERMES_PM_KEY` con valor aleatorio, `DB_PATH=/data/hermes_pm.sqlite3`, y montar un volumen persistente en `/data` antes de activar el modo automático. Sin volumen se pierden datos al redesplegar. Mantener una sola réplica por el proceso de polling. `PORT` lo asigna Railway. El endpoint de salud es `/health`.

## Alcance y límites

- Descubrimiento inicial de 20 wallets del leaderboard mensual. El leaderboard tiene sesgo de selección y no demuestra ventaja futura.
- Resumen de hasta 100 posiciones cerradas de una wallet; muestra incompleta, sin ROI ni score predictivo.
- Agregar wallets manualmente, observar trades recientes y habilitar explícitamente paper-copy por wallet.
- Polling cada 90 segundos por defecto; solo nuevas señales BUY con edad <= 300 s. La primera lectura de trades antiguos se descarta para paper. Se simula al mejor ask observado, limitado por tamaño del mejor nivel, tamaño de la operación origen, efectivo, spread, precio y número de posiciones.
- No se ejecutan ventas automáticas ni se calcula P&L. No hay replay histórico con libro de órdenes por segundo, comisiones dinámicas, mercado resuelto, hedge o señal out of sample.
- En caso de interrupción >100 trades por wallet entre sondeos, se pueden omitir operaciones; no apto para estrategias de alta frecuencia.
- Por diseño, esta versión mide latencia `seen_at - ts` y guarda fills y rechazos para evaluar el experimento. El precio observado no garantiza fill real.

APIs oficiales: https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings , https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets , https://docs.polymarket.com/api-reference/core/get-closed-positions-for-a-user .
