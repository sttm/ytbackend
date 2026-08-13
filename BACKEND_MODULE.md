# Backend resolver pool — ProducersCenter

## Назначение

Этот FastAPI сервис разрешает YouTube/SoundCloud поиск, playlist, playback,
stream URL и offline download, а также содержит управление и health-check
прокси. Он развёрнут на Render.com. База backend-а используется для proxy health
и search metadata cache; PostgreSQL/Neon может быть задан через
`DATABASE_URL` или `PRODUCERSCENTER_BACKEND_DATABASE_URL`.

## Текущее состояние

- Backend является отдельным Git-репозиторием и чистым worktree.
- PWA знает один `YOUTUBE_RESOLVER_URL`: это должен быть Cloudflare Resolver
  Gateway, а не URL конкретного backend-а. Исходники gateway находятся в
  `services/resolver-gateway`; его ещё нужно развернуть и заполнить pool.
- В backend нет тестового набора и `pytest` не указан в `requirements.txt`.
- Private resolver, proxy-management, catalogue, media и dashboard endpoints
  требуют bearer token. Публичным остаётся только `/api/health`, чтобы gateway
  мог определить capacity node.
- Каждый Render node держит локальный semaphore
  `PRODUCERSCENTER_BACKEND_MAX_CONCURRENT_REQUESTS`; при заполнении
  `/api/health` возвращает `503`, и gateway выбирает следующий node.

## Целевая схема пула Render backends

Несколько независимых Render-инстансов можно использовать как pool capacity, но
не следует отдавать их URL пользователю и просить браузер выбирать сервер.
Клиент не может надёжно реализовать общий semaphore: два клиента всё равно
выберут один свободный сервер одновременно, а секреты придётся раскрыть.

Целевая схема:

```text
PWA → Cloudflare Resolver Gateway → атомарный registry/semaphore → Render node A
                                      │                            → Render node B
                                      └                            → Render node C
```

Gateway — единственный URL, который знает PWA. Он проверяет health и capacity,
выбирает node, добавляет server-to-server secret, передаёт запрос и возвращает
нормализованный ответ. Node никогда не перенаправляет пользователя на URL
другой node.

### Registry и semaphore

Для первой версии registry хранит по каждому node: `id`, `baseUrl`, encrypted
secret, `enabled`, `maxConcurrent`, `inFlight`, `health`, `lastCheckAt`,
`cooldownUntil` и `failureCount`. Список node и их secrets не попадают в PWA.

Atomic acquire/release нужен в общем центральном хранилище. Рекомендуемый
вариант для gateway на Cloudflare — Durable Object; допустимы Redis или таблица
Neon с транзакционным lease. KV сам по себе не подходит для строгого semaphore,
поскольку не даёт нужной атомарности.

Правила выбора:

1. Gateway выбирает healthy node с `inFlight < maxConcurrent`; при равенстве —
   least-loaded/round-robin.
2. Перед отправкой создаётся lease с TTL больше request timeout; по завершении
   lease освобождается. TTL освобождает слот после обрыва запроса или падения
   node.
3. `429 CAPACITY_FULL` означает занятость node, а не ошибку пользователя;
   gateway пытается следующий healthy node.
4. Сетевые timeout/5xx увеличивают failure counter и переводят node в cooldown;
   доменные ошибки YouTube не должны безусловно отключать все node.
5. Если свободных node нет, gateway возвращает единый `503 RESOLVER_BUSY` с
   `Retry-After`; PWA показывает retry, а не внутренние адреса.

Для длительных download/processing задач лучше вернуть `202` и job ID. Слот
держится до terminal state job, прогресс доступен через gateway, cancellation
всегда освобождает lease. Простые search/stream запросы можно оставлять
синхронными с коротким timeout.

Несколько бесплатных Render аккаунтов не отменяют cold start, лимиты egress и
ограничения тарифов. До запуска нужно проверить актуальные правила Render и
измерить cold-start/timeout каждого node; pool должен деградировать корректно,
если часть instance уснула или недоступна.

## Чек-лист Backend и gateway

### P0 — безопасный единый контракт

- [ ] Выбрать и зафиксировать этот FastAPI сервис как каноническую resolver implementation; архивировать остальные варианты.
- [ ] Описать версионированный API contract для search, playlist, stream, playback, download, health, usage и ошибок.
- [ ] Добавить service-to-service auth для всех resolver и proxy-admin endpoints; закрыть dashboard и client proxy export.
- [ ] Установить `debug=false` по умолчанию в production и безопасный CORS allow-list.
- [ ] Добавить pytest, unit/integration tests и CI; минимум проверить timeout, provider error и unauthenticated request.
- [ ] Создать Cloudflare gateway и заменить в PWA один direct Render URL на URL gateway.

### P1 — pool capacity

- [ ] Завести приватный registry node (ID, URL, secret, capacity, enable flag); secrets хранить только в gateway.
- [ ] Реализовать атомарный lease semaphore в Durable Object/Redis/Neon и гарантированный release по `finally` и TTL.
- [ ] Добавить `/api/health` contract с версией, readiness, active jobs и capacity; регулярные health checks из gateway.
- [ ] Реализовать least-loaded selection, retry только на безопасных запросах, cooldown/circuit breaker и `Retry-After`.
- [ ] Сделать jobs для длительных операций: idempotency key, status, cancel, cleanup и timeout.
- [ ] Настроить structured observability: node ID, latency, status class, failure class и capacity; без URL запросов, cookies, proxy credentials и stream URLs.
- [ ] Выполнить нагрузочный тест с одновременными пользователями, падением node и cold start; проверить отсутствие double-dispatch.

## Критерий готовности

Один Render node может быть отключён или заполнен без сбоя пользовательского
потока: gateway выбирает другой здоровый node либо честно возвращает
`RESOLVER_BUSY`. Это и есть корректная реализация требуемого semaphore, а не
переключение серверов в браузере.
