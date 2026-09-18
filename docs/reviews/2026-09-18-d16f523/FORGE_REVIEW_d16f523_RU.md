# Forge 0.11.0 — повторное инженерное review и следующий backlog

**Репозиторий:** `forcewake/forge`  
**Проверяемый snapshot:** `d16f5233c641c490384438d9d0df6b4976b33d64`  
**Предыдущий snapshot:** `e8bf381843894654b24de73c5ea97270043c3c85`  
**Дата review:** 18 сентября 2026 года  
**Режим:** read-only. Репозиторий, issues, pipelines и deployment пользователя не изменялись.

## 1. Вердикт

Это существенно более зрелая реализация, чем предыдущая. Здесь уже есть не только агент, который пишет код, но и исполняемая спецификация запуска, публикационный журнал, отдельный verifier, обработка неопределённых внешних результатов, operator commands, conformance tests и проверка публикуемого container digest.

**Продолжать этот код имеет смысл. Переписывать с нуля или разносить его по независимым репозиториям сейчас не нужно.** Следующий качественный шаг — закрыть разницу между гарантиями общих компонентов и тем, как эти компоненты действительно используются каждым provider/backend/recovery path.

При этом утверждение в CHANGELOG «every finding closed» пока слишком широкое. Оно не согласуется с тем же исходным кодом: GitLab уже потребляет executable RunSpec v3, а GitHub/Azure явно оставлены на v2; helper для guarded transitions существует, но основной controller path по-прежнему используется без него; repository scopes закрывают classic MCP API, но не durable run read model. Это не новые эстетические требования — это незавершённость старых сквозных гарантий.

Есть и новые локализованные дефекты. Например, `.forge-output` по-прежнему hidden directory; три Python permission rules склеиваются в одну строку; полный run ID обходит часть subject filtering в operator commands. Их нужно исправлять небольшими targeted PR, а не ждать большого refactor.

## 2. Что именно проверено

Через GitHub connector закреплён HEAD и просмотрены критические пути lifecycle, публикации, state/claims, CI verification, executable spec, budgets, MCP, operator recovery, harness templates и выбранные tests/release workflows. Между двумя snapshots — **96 commits** по GitHub compare. Это не оценка качества по количеству коммитов.

GitHub Actions для данного HEAD:

- CI run `35356741229`: success. В логе Python 3.13 job `105637831435` — **2601 passed, 4 skipped in 154.82s**.
- В этом push-run `integration-os` и `release-canary` skipped по условиям workflow. Это не означает, что они никогда не запускались.
- Release run `35356747159`, tag `v0.11.0`: success. Проверены успешные шаги canary опубликованного digest, cosign signing и build provenance.
- Canary release проверяет boot/schema/dependencies/reachability; он не является запуском coding agent через target-project GitHub template. В script допускаются skip/waiver для некоторых previous-image условий, поэтому success внешнего шага не следует трактовать как доказательство каждого optional подшага.

**Локально не выполнялся полный checkout, `uv sync`, полный Forge pytest или пользовательский GitLab/GitHub/Azure deployment.** Код читался через connector. Выполнены семь сокращённых behavioral probes: строковые/predicate checks, модель scheduler и две проверки с настоящим SQLAlchemy/SQLite. Они проверяют конкретную семантику, но не подменяют application e2e. Среда probes: Python 3.13.5, SQLAlchemy 2.0.50; repository pin — SQLAlchemy 2.0.48. Для SAVEPOINT поведение подтверждено документацией SQLAlchemy 2.0.

Файлы evidence:

- `review_manifest.json` — что просмотрено и какие результаты откуда взяты;
- `reproduction_results.json` — фактические результаты сокращённых probes;
- `reproduce_review_findings.py` — воспроизводимый локальный скрипт;
- `test_review_regressions.py` — предлагаемые red regression tests для запуска в окружении Forge, здесь не исполнявшиеся;
- `previous_findings_status.json` — все 32 предыдущих пункта с отдельным статусом проверки, без автоматического объявления pass.

Источники CI: [pinned CI run](https://github.com/forcewake/forge/actions/runs/35356741229), [release run](https://github.com/forcewake/forge/actions/runs/35356747159). Основной snapshot: [d16f523](https://github.com/forcewake/forge/tree/d16f5233c641c490384438d9d0df6b4976b33d64).

## 3. Как теперь устроен Forge

В текущем виде это modular monolith с несколькими слоями, но пока не полностью provider-neutral execution core.

**Вход и scheduling.** Provider webhooks превращаются в команды; durable command scheduling принадлежит Postgres. StepRun получает lease и fence; Redis остаётся вспомогательным механизмом пробуждения и частью legacy event processing. Это правильное направление: потеря cache/queue signal не должна означать потерю accepted command.

**Спецификация и согласование.** GitLab PlanRun формирует executable spec со snapshot задачи/плана, routing/policy/budgets. Approval связан со specification identity. GitHub/Azure ещё используют более старую форму с digests и backend fragments. Внешне команды одинаковые, но неизменность входов пока обеспечивается не одинаково.

**Исполнение.** Builtin proposer создаёт ChangeSet; CLI harness работает в CI, не имеет publisher credentials и отдаёт candidate artifact. В template появились CLI pins, secret gating по выбранному driver, meta schema, digest и usage receipt. Общий runner всё ещё заметно Python/uv-ориентирован, что нормально для ограниченного первого profile, но не следует путать с универсальным toolchain layer.

**Публикация.** Кандидаты проходят общий materialization/validation; появился ValidatedCandidate. GitHub bridge теперь действительно валидирует оба входа, а не только harness path. PublicationIntent живёт до remote HTTP; operation identity не создаётся заново при каждом повторе. Это важное исправление предыдущего review.

**Верификация и finalization.** GitHub/Azure теперь действительно паркуются в waiting_ci, отдельно от harness job. Отсутствие checks не маскируется: появился unverified outcome. Общий consistency/finalization слой унифицирует представление результата. Но proof semantics обязательных проверок и актуального head ещё недостаточны.

**Recovery и operator surface.** Есть status/why-blocked/reconcile/retry и auto-revive. Это повышает эксплуатационную ценность, но вводит новый переход из terminal состояния назад к исполнению. Такой переход должен иметь тот же уровень ownership/idempotency, что первоначальная публикация.

**Release.** Build by digest → pull/canary этого digest → mutable tags → sign/provenance. Это уже реальное улучшение delivery дисциплины, а не текст в ADR.

Схема фактического перехода выглядит так:

```text
Provider command
    → durable StepRun
    → provider RunService
        → plan / approval / executable input [паритет пока частичный]
        → builtin или CI harness
        → candidate validation
        → PublicationIntent + native write
        → independent verification
        → readonly review
        → verified или честно unverified delivery result
```

Нужное направление — не ещё один orchestration framework поверх этого, а устранение provider-specific authority decisions из середины этой цепочки.

## 4. Что из прежнего review действительно улучшено

### 4.1. Candidate и publication boundary

В `integrations/github_flow.py` теперь `publish_proposal()` и `publish_changeset()` идут через bundle/materialization/policy validation; native commit принимает ValidatedCandidate. Старый обход GitHub builtin из R01 в просмотренном bridge устранён. Conformance kit содержит отрицательные candidates и positive manifest control.

Это не означает, что любой internal caller автоматически безопасен. Validation candidate content и разрешение конкретному attempt отправить запись — разные проверки. Первая теперь значительно лучше, вторая ещё требует A04.

### 4.2. AttemptContext и checkpoint recovery

GitLab advance явно использует AttemptContext и разделяет source base для cumulative review от attempt base для очередного repair. Добавлены persistable results, чтобы recovery мог использовать уже полученный результат, а не заново вызывать модель при каждом restart.

Это закрывает существенную часть R06/R07. Новые recovery paths — прежде всего revival — нужно проверять тем же способом, а не считать наследующими эти свойства по факту использования FlowRun.

### 4.3. Бюджетная арифметика

Теперь reservation учитывает **consumed + reserved + unresolved + requested**. Неизвестный расход больше не превращается обратно в свободный остаток. Shape-aware обработка inclusive и disjoint counters — тоже правильное изменение.

Новый SAVEPOINT defect в A10 — не повтор прежней арифметической претензии. Формула исправлена; ошибка находится в конкурентном создании самой budget row.

### 4.4. Реальные process-failure tests

`test_failure_injection_os.py` теперь запускает настоящий `python -m forge.worker` subprocess, использует SIGKILL, отдельные локальные LLM/GitLab stub servers и мигрированную schema. В fixture есть ambiguous commit window: effect применяется, ответ теряется. Прежнее замечание «это только отмена asyncio task» к этому новому suite не относится.

Ограничение другое: snapshot tests нужно иметь для всех важных путей и не объявлять неисполненный nightly suite аттестацией текущего push. Apply-then-drop и delayed-apply — также разные failure windows.

### 4.5. Schema gate и release artifact

`database.py` больше не доверяет условному schema_version=1; gate сопоставляет фактические Alembic heads. Fresh Postgres проходит настоящую migration chain. Release canary работает с опубликованным image digest без source mount. Это существенно сильнее прежнего fresh-create-all smoke.

### 4.6. MCP и harness permissions

Classic MCP tools, resources и prompts получили общий guard, scopes и repository allowlist. CLI installations закреплены версиями; GitHub template выдаёт только credentials выбранного driver. Эти исправления нужно сохранить.

Но область enforcement теперь должна включать и сохранённые данные, а не только прямые provider calls: durable run tools остались вне repository allowlist. Это A06.

Источники: [GitHub bridge](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/integrations/github_flow.py), [budget](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/durable/budgets.py), [OS tests](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/tests/test_failure_injection_os.py), [database gate](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/database.py), [release](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/.github/workflows/release.yml).

## 5. Самые важные оставшиеся дефекты

### 5.1. GitHub verification: «не известная ошибка» всё ещё означает «проверено»

После ожидания завершения workflow runs код считает failed только четыре conclusions. Всё остальное без pending попадает в `ready_evidence(True, ...)`. Следовательно, completed/skipped или completed/neutral дают verified, хотя не доказывают выполнение требуемых тестов.

Отдельно в этом решении не используются обязательные job identities из configuration. Успешный docs workflow не заменяет обязательный tests workflow. Исключение harness по `r['name'] == configured_workflow` также сравнивает display name с filename, которые обычно не равны.

Здесь уже не требуется снова менять всю state machine. Unverified outcome добавлен правильно. Нужно ужесточить только смысл **verified**: положительное доказательство разрешённых producers, нужных checks, нужного attempt и нужной revision.

Отменённый или timed-out workflow нельзя автоматически отправлять в code repair: текущая ветка так делает. Это расходует лимит исправлений на события, которые сами по себе не доказывают ошибку кода.

Ещё один обязательный check — актуальный head после длительного LLM review. Записать candidate SHA в evidence и прочитать его назад не означает проверить, что PR всё ещё указывает на него.

**Probe P03** воспроизводит inspected predicate. Не выполнялись настоящие Actions checks. Полный regression должен вызывать `evaluate_waiting_ci_one()` над fake provider с отсутствующим required test, skipped status и human push во время review.

### 5.2. Есть executable spec — но пока не одинаково на трёх providers

В `service.py` оставлен точный комментарий: GitLab freezes v3, GitHub/Azure keep pre-executable v2 until later wave. Это важнее общего заявления CHANGELOG о закрытом R04.

На GitHub builder ещё хранит digests и backend config без полного immutable input. Routing/verification settings читаются live. Numeric budget profiles и capability-aware planner selection, добавленные для GitLab, автоматически на этот путь не переносятся. В `_compile_harness_selection()` GitHub всё ещё присутствуют `SHIPPED_DRIVERS` и `None` вместо task-aware proposal/реального manifest.

Практический критерий закрытия: создать plan, одобрить, изменить global model/target/jobs/limits и перезапустить worker. Каждый provider обязан либо продолжить ровно approved spec, либо явно потребовать reapproval — но не собрать hybrid старого approval и новых settings.

### 5.3. Bound comment — улучшение, но не immutable brief

GitHub plan_note_id исправляет выбор чужого плана и проблему `[bot]`. Однако `validate_plan_comment()` не сравнивает текст с approved content digest. Comment может остаться тем же объектом с тем же author/header/run-id, но получить другой body. Issue body по-прежнему читается live.

Azure `fetch_workitem()` вообще продолжает сканировать комментарии. Поэтому R05 корректнее считать частично закрытым.

Для v3 нужен immutable BriefEnvelope. Его можно передавать через artifact reference/scoped read endpoint; для небольших payload годится и прямой transport. Не требуется сложный новый distributed storage: требуется проверяемая привязка **байтов входа**, а не лишь имени/ID объекта.

### 5.4. Queue claim не равен сквозному праву сделать effect

Новый `transition_guarded` — правильный primitive. Но `Controller.transition()` по-прежнему делает обычный ORM read/check/write и не делегирует guarded variant. Просмотренные services продолжают им пользоваться. Ambient ExecutionClaim полезен лишь там, где boundary действительно проверяет owner/fence/attempt; чтение одного cancellation_generation этого не заменяет.

Сокращённая SQLAlchemy проверка P07 показывает смысл риска: две sessions читают reviewing; одна фиксирует cancelled; вторая со stale object фиксирует ready_for_human. Это не доказательство частоты такой гонки в deployment, но прямое следствие используемого update pattern без predicate на version.

Кроме обычного transition нужно привести к той же модели revival, finalization и публикацию из reconciler. Проверка в начале долгой materialization не заменяет арбитраж непосредственно перед dispatch. После отправки HTTP уже нельзя обещать ретроактивно отменить remote effect; нужно сохранять его certainty и superseded evidence.

### 5.5. Batch leases могут истечь до начала работы

`claim_due_steps()` выдаёт до пяти задач с leases на 120 секунд. `run_due_steps()` затем делает последовательный `for`, а heartbeat стартует только внутри обработки очередной задачи.

Пример допустимого расписания:

```text
00:00  A claim-ит steps 1..5.
00:00  A выполняет step1; heartbeat продлевает только его.
02:10  Leases steps2..5 истекли; reaper/worker B получают их.
02:30  A завершает step1 и начинает свой старый step2.
```

У A нет initial lease validation перед входом handler; ближайший heartbeat ещё впереди. Это не оптимизация throughput, а корректность ownership.

Простой фикс: sequential worker claim-ит одну задачу. Для параллельности — claim только под свободные slots и запуск supervised heartbeat сразу. При ошибке DB heartbeat должен fail closed, а не просто умереть отдельной task, оставив handler выполнять effects.

### 5.6. Два object-level authorization gap

**MCP run mirror.** Scoped token для repo A не должен читать сохранённый plan/evidence repo B. Сейчас `tools_runs.py` проверяет `forge:read`, но не применяет repo_patterns. `run_list` делает общий SELECT; `run_get`, `plan_get` и `run_evidence_get` читают произвольный run_id. Авторизация direct GitLab reads это не покрывает.

**Operator full-ID resolution.** В `resolve_status_target()` и `resolve_retry_target()` полный ID обрабатывается отдельной веткой: provider и issue_iid проверяются, project_id — нет. Bare/prefix branch имеет другой, более строгий query. На GitLab два проекта могут иметь issue #7; полный ID чужого run проходит resolver. Status-команда может вынести чужое состояние в текущий issue; retry смешивает переданный project с найденным run.

Не требуется угадывать, какие remote writes реально пройдут после такого смешивания: уже неверный выбор объекта — дефект. Impact зависит от command access, наличия known ID и прав approver. Review не отправлял unauthorized requests.

### 5.7. Два небольших дефекта в shipped harness recipe

**Hidden artifact staging.** Имя `.forge-output` начинается с точки. Комментарии «NON-HIDDEN» не меняют поведение filesystem/glob. `upload-artifact@v4` выставляет excludeHiddenFiles, toolkit пропускает dot-directory до обхода. Нужно `forge-output` без точки либо явное include-hidden-files:true для ограниченного staging. Новые meta/digest/usage fields сохраняются.

**Python tool grants.** Соседние литералы:

```python
"Bash(python3:*)"
"Bash(python:*)"
"Bash(.venv/bin/python:*),..."
```

склеиваются в `Bash(python3:*)Bash(python:*)Bash(.venv/bin/python:*)`. Это не три entries. Правила нужно хранить списком и сериализовать, а pinned CLI проверять на минимальных allow/deny cases.

P01/P02 проверяют source/library predicate и Python literal semantics. Они не являются реальным запуском upload-artifact или Claude CLI.

### 5.8. SAVEPOINT существует слишком поздно

В `open_budget()` объект добавлен в session перед `begin_nested()`. SQLAlchemy делает flush pending state перед созданием savepoint. При конфликте уникальности outer transaction получает ошибку, после чего except пытается читать winner из inactive session.

Сокращённая проверка P06 на настоящем SQLAlchemy получила `PendingRollbackError`. Перенос add внутрь nested block позволяет прочитать winner. Для production лучше выбрать Core `INSERT ... ON CONFLICT`, а регрессию проверить двумя AsyncSessions на PostgreSQL.

Это не основание отменять новую budget model. Наоборот, модель хорошая; нужна корректная конкуренция её initialization.

## 6. Recovery: что важно проверить сверх happy path

PublicationIntent — правильное улучшение. Stable operation_key плюс parent probe позволяет принять уже применённый commit после потери ответа. Но **negative probe не доказывает, что предыдущая запись больше не исполняется**.

В GitLab `_recover_open_intent` zero hits + unchanged head трактует как возможность повторно отправить запись. Публикационный marker не является upstream idempotency key. Если первый запрос принят, но применение задержано, он может закончиться после negative probe и нового POST. Этот сценарий отличается от текущего apply-then-drop fixture.

Не нужно обещать универсальный exactly-once поверх любой API. Нужно явно определить guarantees native adapters и conservative unknown policy. Там, где есть native preconditions/CAS, использовать их; там, где отсутствие эффекта нельзя доказать, не называть новый POST безопасным только из-за одного чтения head.

Новые `/retry` и auto-revive также нуждаются в собственном durable attempt. Сейчас revival transition обычный read/check/write, а backend dispatch идёт после него. Два reconcilers или crash между transition и redispatch — отдельные failure windows. Исправление: новый attempt, CAS transition и scheduled step в одной транзакции; повтор command event не должен ещё раз увеличивать cycles.

Инфраструктурная ошибка, ошибка кода, policy denial и неопределённый external effect — разные типы. Free-text regex можно использовать для диагностической подсказки, но не как единственный источник решения повторить side effect.

## 7. Как оценивать новые функции

**Operator UX — наиболее полезное расширение.** `/status`, `/why-blocked`, `/reconcile` и продолжение existing branch решают реальную проблему эксплуатации. После исправления scope и durable revival это даст больше пользы, чем новый provider. Желательно, чтобы команда показывала не только status_reason, но и candidate/attempt, remaining/unresolved budget, last native operation и ровно доступное следующее действие.

**Task-aware selection — пока частично provider-specific.** Pure compiler и numeric budget profiles хороши. До общего PlanRun это не нужно рекламировать как одинаковую функцию всех providers. Manifest должен описывать действительно доступные capabilities, а planner — выбирать внутри них, не создавать новые permissions.

**Codegraph-aware brief — разумная опция, не обязательный новый центр архитектуры.** В template видны version pin, отключение telemetry и исключение graph state из candidate. Оценивать его стоит на bounded cohort: снижает ли repeated reads и human rework. Сам факт наличия графа не доказывает качество решения; индекс не должен расширять tool authority.

**Security triage governance.** Конфигурация теперь разделяет suggestion, authoritative confirmation и default-off autoaccept/remote dismissal. Это правильная граница. Полный concurrent ingest/triage/dismiss workflow здесь независимо не исполнялся; не ставлю ему ни новый defect, ни безусловный pass.

**Release provenance — существенное улучшение.** Published digest реально является canary target, а signing/provenance шаги прошли. Нужно только связать release claims с теми tests, которые действительно выполнены: boot smoke не подтверждает agent lane, а зелёный unit suite не подтверждает не запущенный OS FI.

## 8. Архитектурный план без очередной переписи

### Следовать собственному ADR-0027

ADR честно описывает incremental consolidation и завершённый FinalizeEvidence slice. Именно его следует продолжать. Не требуется удалять все services одним PR.

Рекомендованный порядок общих UseCases:

1. **Verified RunSpec / ApproveDecision.** Один executable input, одна validation/migration policy, один budget/capability resolver.
2. **Attempt ownership / publication authorization.** Claim, attempt generation, cancellation, PublicationIntent и native write contract.
3. **ObserveVerification / FinalizeEvidence.** Один required-check proof contract и freshness rules; adapter только переводит native evidence.
4. **Retry / Revival / Reconcile.** Одна авторизация, одна attempt history и один способ scheduled recovery.

Provider adapter хранит auth, native IDs, paging, branch API semantics, PR/MR representation, CI evidence translation. Он не решает по-своему, что означает approved или verified.

Harness driver хранит invocation, tool grants, pinned CLI version, normalization events/usage и workspace output. Он не выбирает repository authority и не публикует remote commits. BYOK остаётся credential/model-route policy, не четвёртой формой workflow.

До появления независимых maintainers/release cycles достаточно одного repo и одного release train. Физические Python packages полезны после import-contract checks; отдельные сервисы и много Git repos сейчас усложнят coordinated fixes.

### Не менять goalposts

Новый JSON `A01–A18` не означает, что прежние 32 пункта снова не сделаны. В нём отдельно отмечены incomplete fixes, новые defects, reliability risks и будущие product investments. Старые R12 arithmetic, R22 schema gate и реальный OS-process suite не нужно переписывать заново. Нужны точные исправления и расширение coverage на ранее непроверенные составные сценарии.

## 9. Какой следующий release имеет смысл

Я бы не делал его релизом «ещё больше agents». Его цель:

> Один и тот же approved task, один и тот же budget и один и тот же verification contract дают одинаковые гарантии независимо от provider, harness и способа recovery.

Практические exit conditions:

- scoped principal и command context не могут выбрать/прочитать чужой run;
- все новые provider runs используют один executable spec либо честно маркируются legacy/unverified;
- required checks не заменяются optional green/skipped workflow;
- stale worker не получает право на новую publication;
- every claimed task исполняется только пока принадлежит worker;
- неизвестный external effect не дублируется на основании недостаточного probe;
- shipped target harness recipe реально загружает и возвращает candidate;
- release manifest отличает performed, skipped, limited и unsupported.

После этого следующий полезный продуктовый шаг — **12–20 bounded tasks для сравнения accepted work**, затем versioned execution profiles. Число cohort — предлагаемая организация pilot, не статистически откалиброванный размер и не гарантия P90.

Для throughput сохраняется предыдущая дисциплина: logical input/cache traffic не делится на output tokens/s. Измеряются per-call latency/TTFT, inclusive/disjoint usage, active execution, CI wait и human review. Итоговая метрика — время и расход на принятую задачу с учётом всех failed/superseded attempts.

## 10. Источники и границы внешней проверки

Основные выводы основаны на pinned source. Внешние материалы использованы только для проверки конкретных API/library contracts:

- [SQLAlchemy 2.0 Session Basics — flushing/SAVEPOINT](https://docs.sqlalchemy.org/en/20/orm/session_basics.html): begin_nested flushes pending changes до SAVEPOINT; session после failed flush требует rollback.
- [GitHub Checks REST guide](https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-checks): завершение и success — разные величины, conclusions имеют несколько значений.
- [upload-artifact v4 search.ts](https://github.com/actions/upload-artifact/blob/v4/src/shared/search.ts): excludeHiddenFiles зависит от includeHiddenFiles.
- [actions toolkit globber](https://github.com/actions/toolkit/blob/main/packages/glob/src/internal-globber.ts): hidden basename исключается до обхода directory. Этот внешний ref подвижен; review фиксирует увиденную семантику, не заявляет локальный запуск bundled v4 binary.

Полные code references и criteria расположены в каждой задаче ниже и в JSON. Никакие реальные private credentials в probe fixtures не использованы.

---

# Backlog A01–A18

Приоритеты — инженерный порядок исправлений, не CVSS. P1 применяется к указанному affected path/deployment condition; не каждый пункт означает безусловную уязвимость любого single-user запуска. P2 — последующий hardening, conformance и продуктовая работа.


## A01 · P1 · Сделать verification положительным доказательством выполнения обязательных checks

Тип: `incomplete_fix`. Связь с прежним review: R02, R21.

**Что найдено.** GitHub ждёт независимые workflow runs, но verified=True вычисляется как отсутствие pending и четырёх failure conclusions. Required jobs в этом решении не проверяются; имя workflow сравнивается с конфигурируемым filename. В surface записываются name/conclusion, а не полная identity проверки. Отсутствие CI теперь честно unverified — это исправлено.

**Когда проявляется.** Единственный completed/skipped workflow или успешный documentation workflow при отсутствующем обязательном tests получает verified. Cancelled/timed_out направляются в code repair. Human push во время review требует отдельной проверки актуальности.

**Граница воздействия.** GitHub verification path; аналогичные negative contract tests обязательны для остальных providers.

**Исправление.**

1. Ввести общий VerificationResult с required check identities, producer, native run/attempt, subject_head_oid, tested_oid, target_base_oid и completeness.
2. Разрешать success только по явному контракту; skipped/neutral — только с явной policy waiver, неизвестное — unknown. Проверять обязательные, а не все произвольные jobs.
3. Исключать execution workflow по workflow_id/path + attempt, не по display name. Не классифицировать cancel/timeout автоматически как ошибку кода.
4. Перед verified-ready перечитывать actual head и cancellation generation; не назначать tested_oid автоматически равным candidate без свидетельства провайдера.

**Критерии приёмки.**

- Required tests отсутствует: verified=False даже при зелёном docs.
- completed/skipped, neutral, None/неизвестный conclusion не становятся verified без policy waiver.
- Cancel, timeout, unreadable logs не вызывают code repair без evidence ошибки кода.
- Human push во время LLM review инвалидирует candidate-specific результат.
- Synthetic-merge и source-head evidence не смешиваются; producer и attempt сохраняются.

**Зависимости:** нет обязательных.

**Локальный probe:** P03; см. границы проверки в results JSON.

**Символы:** `GitHubRunService.evaluate_waiting_ci_one`, `AzureRunService.evaluate_waiting_ci_one`, `_review_and_ready`.

**Исходники:** [src/forge/runs/github_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/github_service.py) · [src/forge/runs/azure_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/azure_service.py) · [src/forge/runs/verification.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/verification.py)

## A02 · P1 · Подключить executable RunSpec, budgets и capability routing к GitHub и Azure

Тип: `incomplete_fix`. Связь с прежним review: R04, R13, R31.

**Что найдено.** GitLab потребляет executable schema v3; комментарий RUN_SPEC_SCHEMA_VERSION прямо оставляет GitHub/Azure на v2. Их builder хранит digests и часть backend config, но не полный approved input и enforceable numeric budgets. GitHub compiler всё ещё получает SHIPPED_DRIVERS и planner_proposal=None.

**Когда проявляется.** Одинаковая настройка бюджета/разрешённых драйверов и последующая смена model/target/workflow дают неодинаковые гарантии в зависимости от provider.

**Граница воздействия.** GitHub/Azure parity; не обесценивает уже работающий GitLab v3 path.

**Исправление.**

1. Потреблять один typed verified spec во всех start/go/retry/reconcile/review legs.
2. Создавать бюджет до первого оплачиваемого вызова каждого provider; CLI обозначать partial enforcement, не обещать hard token cap без interception.
3. Перенести capability manifest и structured planner selection в общий PlanRun; post-gate live settings не должны менять approved execution profile.
4. Для legacy v2 установить явную migration/reapproval policy вместо молчаливого исполнения как v3.

**Критерии приёмки.**

- Созданные на трёх providers runs содержат полный v3 документ и numeric limits из одного profile.
- После /go изменение Settings не меняет approved model/target/required jobs/budget.
- Budget exhausted: ни один adapter не делает model/episode dispatch.
- Available-drivers manifest из одного разрешённого драйвера не может породить другой harness.
- Legacy replay не выдаёт себя за исполнение verified v3 spec.

**Зависимости:** нет обязательных.

**Символы:** `RUN_SPEC_SCHEMA_VERSION`, `_build_run_spec_document`, `_compile_harness_selection`, `load_verified_spec`.

**Исходники:** [src/forge/runs/spec.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/spec.py) · [src/forge/runs/service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/service.py) · [src/forge/runs/github_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/github_service.py) · [src/forge/runs/azure_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/azure_service.py)

## A03 · P1 · Передавать immutable approved BriefEnvelope, а не текущий текст issue/comment

Тип: `incomplete_fix`. Связь с прежним review: R05, R04.

**Что найдено.** GitHub exact plan_note_id и нормализация [bot] исправлены. Но validation проверяет author/header/run substring, не digest текста; issue body читается заново. Azure fetch_workitem всё ещё сканирует latest bot comment с plan.

**Когда проявляется.** После approval меняется body того же comment или issue, либо Azure получает другой подходящий plan comment. Execution input уже не тот, который был одобрен.

**Исправление.**

1. Создать versioned BriefEnvelope с task/plan bytes, run_id, attempt_id, spec_digest и content digest.
2. Поставлять его через scoped read-only artifact/reference; проверять digest в lane до запуска.
3. Комментарии оставить человекочитаемым представлением; exact comment transport допустим только с content hash и immutable task snapshot.
4. Legacy heuristic включать только в явно ослабленном профиле, не default production.

**Критерии приёмки.**

- Изменённый approved comment с теми же author/header/run_id отклоняется.
- Изменение issue после approval не меняет brief текущего attempt.
- Другой run/attempt/digest отклоняется на всех providers; новые issue changes требуют нового approval.

**Зависимости:** A02.

**Символы:** `validate_plan_comment`, `fetch_issue_context`, `fetch_workitem`.

**Исходники:** [src/forge/harness_entry.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/harness_entry.py) · [ci/templates/forge-harness.github.yml](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/ci/templates/forge-harness.github.yml) · [src/forge/runs/spec.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/spec.py)

## A04 · P1 · Довести ExecutionClaim до каждого transition и права отправить side effect

Тип: `incomplete_fix`. Связь с прежним review: R10.

**Что найдено.** ExecutionClaim и guarded mutation добавлены, но просмотренные services продолжают вызывать обычный Controller.transition; он не делегирует guarded CAS. Publisher использует ambient cancellation generation, но не валидирует step lease owner/fence перед native call. Отдельная publication_grant_valid запрещает cancelled, но не все terminal outcomes.

**Когда проявляется.** Worker потерял lease или другой обработчик изменил run, но прежний callback до ближайшего heartbeat продолжает side effects. Stale ORM update может перезаписать terminal status.

**Граница воздействия.** Особенно важно при нескольких workers/reconcilers; reduced SQLAlchemy probe не является production race test.

**Исправление.**

1. Один обязательный guarded mutation API с version/attempt/fence/cancel generation; legacy transition исключить из production routes.
2. Связать StepRun с run при execution binding и валидировать владение перед первым и каждым последующим effect.
3. Persist/арбитрировать publication authorization до native call; после dispatch честно учитывать in-flight/unknown, а не обещать ретроактивную отмену remote commit.
4. Применить ту же дисциплину к retry, auto-revive, reconciler и finalization; late effects — superseded evidence.

**Критерии приёмки.**

- Два конкурирующих transition с одной version: один applied и один StaleClaim.
- Старый lease/fence не делает новый native write даже до heartbeat tick.
- Blocked/failed/cancelled/ready run не публикуется поздним callback без нового разрешённого attempt.
- Cancel до dispatch запрещает отправку; cancel после dispatch сохраняет однозначно описанный uncertain/superseded outcome.

**Зависимости:** нет обязательных.

**Локальный probe:** P07; см. границы проверки в results JSON.

**Символы:** `Controller.transition`, `Controller.transition_guarded`, `Controller.revive_transition`, `publication_grant_valid`, `publish_validated_candidate`.

**Исходники:** [src/forge/durable/controller.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/durable/controller.py) · [src/forge/runs/publisher.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/publisher.py) · [src/forge/worker/steps.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/worker/steps.py) · [src/forge/repository/writer.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/repository/writer.py) · [src/forge/runs/service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/service.py) · [src/forge/runs/github_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/github_service.py)

## A05 · P1 · Не держать batch незапущенных задач на истекающих leases; исправить heartbeat failure

Тип: `new_defect`. Связь с прежним review: новый finding.

**Что найдено.** По умолчанию claim выдаёт пять leases на 120 секунд, а run_due_steps исполняет их последовательно. Heartbeat появляется только у уже исполняемой задачи. Exception при renew завершает heartbeat task, не основной handler.

**Когда проявляется.** Первая задача дольше lease; остальные уже reaped и выданы другому worker, но первый worker затем начинает их со старым claim. При сбое DB handler продолжает без живого lease renewal.

**Исправление.**

1. Claim только под фактически свободные execution slots; простой корректный вариант для sequential worker — limit=1.
2. Для параллельного исполнения использовать bounded tasks и heartbeat на каждую принятую задачу; проверять claim до handler.
3. DB/renewal error должен останавливать дальнейшие side effects; heartbeat task supervisor и awaited teardown.
4. Отличать shutdown cancellation от lease-loss, не проглатывать CancelledError всей очереди.

**Критерии приёмки.**

- Batch=5, первый handler дольше120s, два workers: каждый последующий handler входит один раз с актуальным owner.
- Renewal exception исключает любые последующие writes и оставляет recoverable step.
- SIGTERM не начинает следующую заранее claimed задачу.

**Зависимости:** A04.

**Локальный probe:** P05; см. границы проверки в results JSON.

**Символы:** `STEP_CLAIM_BATCH`, `run_due_steps`, `execute_claimed_step._heartbeat`.

**Исходники:** [src/forge/worker/steps.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/worker/steps.py)

## A06 · P1 · Ограничить durable MCP run tools теми же repository scopes

Тип: `incomplete_fix`. Связь с прежним review: R19.

**Что найдено.** Classic tools/resources/prompts получили scope + repo guards. Durable run tools проверяют forge:read, но не repo_patterns: run_list выбирает общий набор, остальные читают произвольный run_id.

**Когда проявляется.** Токен с allowlist только repo A читает сохранённые runs/планы/evidence repo B. Угадать ID не обязательно: run_list выдаёт их.

**Граница воздействия.** Authenticated scoped deployment с несколькими repos; не утверждение об anonymous access.

**Исправление.**

1. Разрешать доступ по canonical subject самого run, а не caller-supplied provider filter.
2. Фильтровать run_list на уровне query/policy с корректной пагинацией; get/plan/evidence проверять owner subject до сериализации.
3. Общая object-level authorization для всех read models; unauthorized/not-found не должны раскрывать лишние метаданные.

**Критерии приёмки.**

- Token(A) не получает B через каждый из четырёх tools, включая явный ID.
- Попытка provider/status фильтром расширить область не работает.
- Тест использует реальный MCP request context и зарегистрированные tools, не только helper.

**Зависимости:** нет обязательных.

**Символы:** `run_list`, `run_get`, `plan_get`, `run_evidence_get`, `repo_target_allowed`.

**Исходники:** [src/forge/mcp_server/auth.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/mcp_server/auth.py) · [src/forge/mcp_server/server.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/mcp_server/server.py) · [src/forge/mcp_server/tools_runs.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/mcp_server/tools_runs.py)

## A07 · P1 · Применять полный subject scope и для 32-символьного run ID

Тип: `new_defect`. Связь с прежним review: R03, R29.

**Что найдено.** Ветвь полного ID проверяет provider и issue_iid, но не project_id; query для bare/prefix IDs фильтрует project_id. GitLab handler передаёт project_id текущего комментария дальше, не повторяя проверку matched run.

**Когда проявляется.** Run из project202 issue7 упомянут полным ID в project101 issue7: resolver принимает его. Status может вывести чужое состояние; authorized retry смешивает run identity и target project.

**Граница воздействия.** Нужен доступ к команде и known ID; права retry отдельно ограничены approvers.

**Исправление.**

1. Всегда выполнять один scoped query с provider/connection/project/repo/subject, добавляя equality/prefix condition для requested ID.
2. После resolution downstream target получать из проверенного subject, не сочетать run от одного контекста с project другого.
3. Те же проверки для status, why-blocked, retry, reconcile и approval.

**Критерии приёмки.**

- Два projects с issue_iid=7: bare/prefix/full-ID resolution одинаково отказывает cross-project запросу.
- Rejected command: ноль model calls, commits и чужих metadata в ответе.
- Legitimate authorized retry в своём project остаётся работоспособным.

**Зависимости:** нет обязательных.

**Локальный probe:** P04; см. границы проверки в results JSON.

**Символы:** `resolve_retry_target`, `resolve_status_target`, `handle_retry_note`, `handle_status_note`.

**Исходники:** [src/forge/runs/revival.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/revival.py) · [src/forge/runs/service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/service.py)

## A08 · P1 · Исправить staging path GitHub artifact: убрать ведущую точку

Тип: `failed_fix`. Связь с прежним review: R16.

**Что найдено.** Шаблон называет .forge-output non-hidden и передаёт path: .forge-output без include-hidden-files. Это по-прежнему hidden directory. upload-artifact v4 включает excludeHiddenFiles по умолчанию; glob отбрасывает dot-directory до обхода.

**Когда проявляется.** Fresh repo, использующий shipped template как написано: candidate files созданы, upload не находит их. Доказательство на уровне template/library contract, не live запуска user deployment.

**Граница воздействия.** Shipped GitHub harness recipe; отдельно применённый исправленный workflow мог отличаться.

**Исправление.**

1. Использовать forge-output/ без точки и обновить все producer/consumer/exclude refs; либо явно include-hidden-files:true только для clean allowlisted staging.
2. Добавить runtime artifact canary на точный template revision, не лишь assert текста YAML.
3. Сохранить новые meta schema/diff digest/usage поля — эта часть улучшения полезна.

**Критерии приёмки.**

- Реальный upload/download минимального candidate в отдельном тестовом workflow проходит.
- В ZIP ровно contract files, нет auth.json, .env, .venv или control directory.
- Fixture потребляется тем же CandidateArchive parser.

**Зависимости:** нет обязательных.

**Локальный probe:** P01; см. границы проверки в results JSON.

**Символы:** `Emit candidate artifact`, `Upload candidate`, `--emit-meta`.

**Исходники:** [ci/templates/forge-harness.github.yml](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/ci/templates/forge-harness.github.yml) · [src/forge/harness_entry.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/harness_entry.py)

## A09 · P2 · Собирать Claude allowed-tools из списка, а не конкатенацией строк

Тип: `new_defect`. Связь с прежним review: новый finding.

**Что найдено.** Между литералами Bash(python3:*), Bash(python:*) и следующим Bash(.venv/bin/python:*) нет запятых. Python склеивает их в одно значение.

**Когда проявляется.** В сгенерированном списке отсутствуют отдельные python3/python правила; агент может получать отказы именно на test commands, ради которых allowlist расширили.

**Граница воздействия.** Строковая ошибка воспроизведена; конкретный verdict CLI локально не тестировался.

**Исправление.**

1. Хранить rules как tuple/list отдельных строк и сериализовать через join.
2. Проверять rendered argv/config и минимальные allowed/denied command cases на pinned CLI.
3. Не считать разрешение python безопасной песочницей: security boundary — отсутствие write creds и execution isolation.

**Критерии приёмки.**

- Три ожидаемых правила присутствуют отдельно в parsed rendered config.
- Pinned CLI допускает test command; deny git push сохраняется.
- Malformed/unknown permission profiles не молча исполняются.

**Зависимости:** нет обязательных.

**Локальный probe:** P02; см. границы проверки в results JSON.

**Символы:** `_CLAUDE_ALLOWED_TOOLS`, `render_driver_script`.

**Исходники:** [src/forge/harness_entry.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/harness_entry.py)

## A10 · P2 · Исправить SAVEPOINT-порядок при конкурентном создании RunBudget

Тип: `new_defect`. Связь с прежним review: новый finding.

**Что найдено.** session.add(budget) выполняется до begin_nested. SQLAlchemy flushes pending state ДО SAVEPOINT; при duplicate insert outer session оказывается inactive, а except пытается читать winner без rollback.

**Когда проявляется.** Два callers одновременно открывают бюджет одного run: проигравший получает IntegrityError затем PendingRollbackError вместо idempotent adoption.

**Граница воздействия.** Reduced test выполнен на SQLAlchemy2.0.50 sync/SQLite; repo pin2.0.48 async/Postgres проверять в regression suite.

**Исправление.**

1. Предпочесть INSERT ON CONFLICT с read-back либо перенести add внутрь begin_nested при чистом outer session.
2. Проверить аналогичные SAVEPOINT patterns в проекте; не гасить IntegrityError глобальным except.
3. Сохранить исправленную формулу consumed+reserved+unresolved: это другой слой проблемы.

**Критерии приёмки.**

- Два actual AsyncSessions/Postgres открывают один run budget; оба получают одну row identity, outer transaction остаётся usable.
- Контрольный duplicate с add-before-savepoint воспроизводит прежний дефект.
- Соседние audit/state writes не теряются из-за rollback.

**Зависимости:** нет обязательных.

**Локальный probe:** P06; см. границы проверки в results JSON.

**Символы:** `open_budget`.

**Исходники:** [src/forge/durable/budgets.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/durable/budgets.py)

## A11 · P1 · Сделать retry/auto-revive durable и однократными переходами

Тип: `new_risk`. Связь с прежним review: R29, R10, R07.

**Что найдено.** Revival — новый переход из terminal в proposing. revive_transition остаётся ORM read/check/write. Возобновление и redispatch разделены; handler пишет acknowledgement и вызывает backend после commit перехода. Explicit retry увеличивает cycle; источник retryability часто текстовая причина.

**Когда проявляется.** Два reconcilers revive один run или процесс умирает после persisted proposing до dispatch. Неизвестный remote effect ошибочно классифицирован как простой transient и запускается повторно.

**Исправление.**

1. Записывать RevivalIntent/Attempt и scheduled step в одной CAS-транзакции; command delivery ID делает повтор /retry идемпотентным.
2. Типизировать retryability отдельно от effect certainty; неизвестную publication сначала reconcile, не перевыполнять.
3. Новый retry не сбрасывает spent/unresolved budget; превышение approved cycles требует нового budget/approval decision.
4. Сохранять previous attempt evidence и постоянный target identity.

**Критерии приёмки.**

- Два workers и одно recovery window: один new attempt, один dispatch.
- SIGKILL после revive commit до backend call автоматически восстанавливается scheduled step.
- Повтор того же /retry event не увеличивает cycles второй раз.
- Cancel всегда запрещает auto-revive; unknown effect не делает blind redispatch.

**Зависимости:** A04, A07, A12.

**Символы:** `evaluate_revivals`, `_begin_auto_revive`, `Controller.revive_transition`, `handle_retry_note`.

**Исходники:** [src/forge/runs/revival.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/revival.py) · [src/forge/durable/controller.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/durable/controller.py) · [src/forge/runs/service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/service.py)

## A12 · P1 · Не приравнивать negative probe к доказанному отсутствию pending remote effect

Тип: `reliability_risk`. Связь с прежним review: R11.

**Что найдено.** Persisted intent/stable key/marker+parent adoption реально добавлены. Но zero hits + unchanged head разрешают redispatch; GitLab writer не передаёт branch-wide CAS, а marker сам по себе не является provider dedupe. Negative read не исключает ещё исполняющуюся первую запись.

**Когда проявляется.** Provider принял A, применяет её медленно. Worker потерял ответ; probe пока видит old head. B отправлена; затем A и B завершаются. Это отличный от apply-then-drop сценарий текущего OS fixture.

**Исправление.**

1. Добавить effect-certainty state; отдельно описать гарантию каждого native adapter.
2. Для неопределённого in-flight GitLab effect не называть повтор safe только по одному read; использовать подтверждённые native preconditions/детерминированную операцию либо консервативно парковать unknown.
3. Арбитрировать dispatch intent ownership; marker и manifest digest сохранять для reconciliation, а не представлять как upstream idempotency key.

**Критерии приёмки.**

- Fake provider умеет принять запрос, задержать САМО применение до negative probe и завершить после повторной попытки.
- Не появляется второй logical candidate/несогласованная история; невозможность доказать отсутствие эффекта остаётся visible unknown.
- Apply-then-drop test продолжает проходить, но не считается достаточным для всех unknown windows.

**Зависимости:** A04.

**Символы:** `_recover_open_intent`, `ProbeVerdict.REDISPATCH`, `_dispatch`.

**Исходники:** [src/forge/repository/writer.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/repository/writer.py) · [src/forge/durable/intents.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/durable/intents.py) · [tests/test_failure_injection_os.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/tests/test_failure_injection_os.py)

## A13 · P1 · Не расширять project scope при ошибке чтения конфигурации

Тип: `incomplete_fix`. Связь с прежним review: R14.

**Что найдено.** В просмотренных start paths ошибка config load обрабатывается ProjectConfig() с пустым allowed_paths. Typed authoritative file reads улучшены, но конфигурационная authority ещё может перейти в whole-repo defaults при неизвестном результате чтения.

**Когда проявляется.** Transient401/403/5xx или malformed restricted config приводит не к отказу, а к run без path restriction.

**Исправление.**

1. Различать confirmed_absent, valid config, unreadable, invalid; только confirmed_absent допускает явно утверждённый default profile.
2. Разрешения могут сужаться, но не расширяться из-за transport/parser error.
3. Заморозить config provenance/ref/digest в executable spec.

**Критерии приёмки.**

- Restricted config + read timeout/403/invalidYAML: ноль paid calls и ноль commits до разрешения состояния.
- Подтверждённый404 обрабатывается документированным default policy, не маскируется общим except.
- Перезапуск использует approved config snapshot.

**Зависимости:** A02.

**Символы:** `start_run`, `load_project_config`.

**Исходники:** [src/forge/runs/service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/service.py) · [src/forge/runs/github_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/github_service.py) · [src/forge/runs/azure_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/azure_service.py) · [src/forge/orchestrator/project_config.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/orchestrator/project_config.py)

## A14 · P2 · Проверять комбинации invariant failures в реальных service legs

Тип: `test_expansion`. Связь с прежним review: R21, R20, R30.

**Что найдено.** Conformance kit теперь вызывает реальные transport/service legs над fakes, а OS suite использует subprocess — это исправлено. Но текущие найденные combined cases отсутствуют: scoped token→run mirror, required job absent+green optional, leased batch, pending remote application, comment same-ID edit.

**Когда проявляется.** Helper-level тест зелёный, но composed runtime использует v2 или другое policy path. Release canary boot/migrate не выполняет target harness upload.

**Исправление.**

1. Параметризовать negative contracts по provider × backend × direct/worker/recovery path.
2. Держать быстрые deterministic cases на PR; OS/target-harness canary nightly или release-gate по затронутым областям.
3. Release evidence связывать с тем же SHA/digest; skipped или старый successful OS run не считать новой аттестацией.
4. Проверять реальные effects и actual command execution, а не только строки комментариев/docstrings.

**Критерии приёмки.**

- Каждый A01–A13 имеет failing-before/passing-after regression на production entry point.
- Capability contract не может стать attested, если хотя бы один mandatory path не тестировался.
- Публикуется machine-readable manifest tests/sha/digest/skips/profile.

**Зависимости:** A01, A04, A06, A08.

**Символы:** `ProviderConformanceKit`, `integration-os`, `release-canary`.

**Исходники:** [tests/test_conformance.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/tests/test_conformance.py) · [tests/test_failure_injection_os.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/tests/test_failure_injection_os.py) · [.github/workflows/ci.yml](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/.github/workflows/ci.yml) · [.github/workflows/release.yml](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/.github/workflows/release.yml)

## A15 · P2 · Продолжить ADR-0027: общий semantic core без big-bang rewrite

Тип: `architectural_followthrough`. Связь с прежним review: R27.

**Что найдено.** Фактически общим стал FinalizeEvidence/formatting; ADR сам перечисляет оставшиеся slices. Три services ещё владеют существенной lifecycle логикой, и найденная parity drift это подтверждает.

**Когда проявляется.** Следующий feature fix снова меняется в трёх местах и закрепляется несовместимыми tests.

**Исправление.**

1. Извлекать UseCase по одному: VerifiedSpec/Approval → Attempt/Claim → ObserveVerification → Retry/Revival.
2. Providers оставить native transport/auth/identity/CI evidence adapters; не стирать различия CAS.
3. Ввести typed contracts/import boundary tests внутри существующего src/forge, потом при необходимости workspace packages.
4. Сохранить monorepo и общий release train; CLI deps изолировать runner images, не плодить сервисы.

**Критерии приёмки.**

- Добавление очередного provider не требует копирования approval/budget/verification state machine.
- Все три adapters проходят тот же application-service suite.
- Persistence/migrations backward compatibility проверены перед удалением legacy methods.

**Зависимости:** A02, A04, A14.

**Символы:** `FinalizeEvidence`, `ObserveVerification`, `ApproveDecision`, `PublishCandidate`.

**Исходники:** [docs/adr/0027-lifecycle-consolidation.md](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/docs/adr/0027-lifecycle-consolidation.md) · [src/forge/runs/consistency.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/consistency.py) · [src/forge/runs/service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/service.py) · [src/forge/runs/github_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/github_service.py) · [src/forge/runs/azure_service.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/azure_service.py)

## A16 · P2 · Генерировать capability/status claims из проверяемого release manifest

Тип: `documentation_contract`. Связь с прежним review: R28.

**Что найдено.** CHANGELOG заявляет every finding closed, хотя source явно оставляет GitHub/Azure v2 и later wave; README status/version/test count отстают от0.11.0/2601.

**Когда проявляется.** Новый пользователь переносит guarantee с GitLab на GitHub по общей галочке либо принимает release packaging evidence за весь SDLC e2e.

**Исправление.**

1. Матрица не только supported, но implemented/runtime-wired/tested-on/guarantee/profile limitations.
2. Автоматически сверять версии pyproject/__init__/tag/docs и counts не хранить вручную как постоянную истину.
3. Closure старых findings отмечать по пути provider/backend и уровню evidence, а не existence нового файла.

**Критерии приёмки.**

- Нельзя заявить v3 spec на adapter, который создаёт schema2.
- Release manifest отделяет boot canary, subprocess FI и real-provider LLM e2e.
- Unknown/not_run остаётся явным, не конвертируется в pass.

**Зависимости:** A14.

**Символы:** `Phase B/C/D complete`, `every finding closed`, `Status`.

**Исходники:** [CHANGELOG.md](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/CHANGELOG.md) · [README.md](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/README.md) · [docs/adr/0027-lifecycle-consolidation.md](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/docs/adr/0027-lifecycle-consolidation.md) · [src/forge/runs/conformance.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/conformance.py)

## A17 · P2 · Добавить bounded delivery evaluation cohort, а не ещё один общий agent benchmark

Тип: `next_feature`. Связь с прежним review: R24, R23.

**Что найдено.** Предложение следующего этапа, не утверждение о дефекте конкретного счётчика: текущая ценность требует оценки качества принятой работы при одинаковых safety contracts.

**Когда проявляется.** Более быстрый harness создаёт больше Draft PR, но требует больше human fixes; tokens/s и READY count скрывают снижение accepted throughput.

**Исправление.**

1. Набор12–20 ограниченных задач: create/update/repair/large file/monorepo scope/tests/infra failure/cancel; это предлагаемый cohort, не измеренная калибровка.
2. Связать all-attempt usage, TTFT/latency, CI wait, active execution, review time и explicit human acceptance.
3. Измерять cost/time per accepted unit; failed/cancelled/superseded расходы сохранять.

**Критерии приёмки.**

- Одинаковые tasks и verification contract для сравнения harnesses.
- Зафиксированы unique unit IDs и independent acceptance, не self-reported agent success.
- Отдельно input/cache/output и latency, без деления total logical traffic на decode tokens/s.

**Зависимости:** A01, A02, A14.

**Символы:** `accepted work package`, `usage receipts`, `verification evidence`.

**Исходники:** [src/forge/runs/consistency.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/consistency.py) · [src/forge/durable/budgets.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/durable/budgets.py) · [docs/adr/0027-lifecycle-consolidation.md](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/docs/adr/0027-lifecycle-consolidation.md)

## A18 · P2 · Стабилизировать один полный execution profile на стек и затем расширять Lego

Тип: `next_feature`. Связь с прежним review: R15, R18, R31, R32.

**Что найдено.** Пины CLI, selected-driver credential gating и codegraph-aware brief добавлены. Это полезная основа; но общий template ещё содержит Python/uv-specific provisioning, live variables и best-effort fallbacks.

**Когда проявляется.** Номинально поддерживаемая комбинация provider×driver не имеет доступных test tools либо запускает отличную от CI версию, расходуя repair budget на окружение.

**Исправление.**

1. Versioned execution profile: image/toolchain/test commands/network/MCP refs/credential capabilities/usage semantics.
2. Сначала end-to-end Python/uv; отдельный .NET profile как расширение после parity. Примеры стеков — рекомендация, не claim текущей поддержки.
3. Codegraph optional: измерять качество на cohort, отсутствие индекса не должно расширять authority или скрывать incomplete evidence.
4. Установка зависимостей deterministic; failed environment bootstrap — infra/config, не code repair.

**Критерии приёмки.**

- Fresh lane запускает тот же build/test contract, что target CI.
- Не выбранные driver credentials отсутствуют; protected publisher credentials не попадают в runner.
- Profile digest сохраняется в approved spec и в execution artifact.
- Новому driver требуется conformance suite, а не новый lifecycle controller.

**Зависимости:** A02, A09, A14, A17.

**Символы:** `driver profiles`, `toolchain bootstrap`, `codegraph integration`.

**Исходники:** [src/forge/harness_entry.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/harness_entry.py) · [ci/templates/forge-harness.github.yml](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/ci/templates/forge-harness.github.yml) · [src/forge/runs/spec.py](https://github.com/forcewake/forge/blob/d16f5233c641c490384438d9d0df6b4976b33d64/src/forge/runs/spec.py)
