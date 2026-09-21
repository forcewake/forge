# Forge 0.15.0: review изменений и переход к работе с системой сервисов

Snapshot: `05868e989a5ab3ae214f905ef0681f224c1dfe5f`, версия **0.15.0**. Сравнение с `44cdaeebf5d7c0148b4b2ee3839edda80d00e846` / 0.14.0. Дата проверки и исследования: **21 сентября 2026**.

## Вывод

Заказчик правильно определил два ограничения текущего `/implement`: planner не исследует исходники инструментами, а approved run не поддерживает управляемое изменение постановки в процессе исполнения. Но вывод «значит, Forge пригоден только для маленьких проектов» слишком сильный. Ограничение определяется масштабом конкретного изменения, наблюдаемостью зависимостей и проверками, а не только количеством репозиториев.

Моя рекомендация — развивать существующий control plane, а не писать новую фабрику: **system-aware discovery → evidence-backed plan → управляемое исполнение с ревизиями → проверка согласованного CandidateSet**. Сначала несколько read-only repositories и один writable target. Затем два/три согласованно изменяемых repository. После этого — репрезентативный pilot в системе из десяти сервисов.

В приложении — английский architecture plan, **64 детализированные задачи**, шесть schema-validated примеров будущих контрактов, source register, closeout прежних D01–D12 и тестовые материалы. Это staged roadmap, не 64 новых дефекта и не требование реализовать всё до первого разговора с заказчиком.

## 1. Что именно проверено

Через GitHub connector зафиксирован SHA, получен diff (три commits, 22 изменённых файла), прочитаны критические paths planning, builtin implementation, config authority, publication, issue-edit/recovery и runtime dispatch. Отдельно исследованы официальные interfaces Claude SDK, Codex App Server, OpenCode, модели durable interaction, catalog и contract/integration testing. [R01–R17, X01–X19]

В upstream Python 3.13 job `106299268377`, CI run `35589127959`, действительно указано **3086 passed, 8 skipped, 270.21 seconds**. CI и отдельный release run `35589129956` завершены успешно. OS-process suite в просмотренном push CI был skipped. Эти результаты не являются моим локальным повторным прогоном. [R13, R14]

Локально выполнены только два ограниченных probes: алгоритм cache identity с объектами, повторяющими поля фактического Azure reader, и абстрактная модель порядка cancel/read/publish. Полный checkout и application pytest здесь не запускались. Предлагаемые application tests проверены на синтаксис, не на исполнение. Ни external writes, ни GitHub issues, ни платные model calls не создавались.

Это risk-oriented review изменённых и связанных участков, не заявление о построчном аудите каждого файла репозитория. Исторические документы и исходный v1-план используются для baseline, но не заменяют текущий код.

## 2. Оценка внесённых исправлений

### Исправления, которые нужно признать закрытыми в проверенном сценарии

**Strict config parsing.** Некорректные nested `forge`, `implement` и `paths` теперь получают typed invalid вместо превращения в пустой unrestricted scope. Это исправление D02 по существу. Не нужно возвращать прежний пример со строковым `paths` как будто код не изменился. [R05]

**Передача scope в GitHub builtin.** Появился `_publish_candidate_run_aware()`, который загружает frozen `allowed_paths` и передаёт их bridge. Прежний D03 — потерянный argument — закрыт. И builtin, и harness path используют этот wrapper. [R04]

**Cancel во время propose.** Добавлена проверка после долгого model call. Предыдущий counterexample с отменой внутри proposer теперь учтён. Ниже остаётся другой участок того же сквозного контракта: чтения внутри bridge после последней проверки. [R04, R07]

**Source OID GitLab harness.** Создание factory branch теперь предпочитает attempt OID, а не имя target branch. Таким образом, сдвиг `main` не должен сам по себе менять исходный snapshot для рассмотренного start path. [R08]

**Manifest absence versus empty.** Resolver/compiler изменены так, чтобы сохранить различие отсутствующей настройки и явно заданного пустого набора. Это правильная форма policy boundary; её надо сохранять в onboarding и всех adapter tests. [R09]

**Cohort и provenance.** Исправления all-attempt aggregate и empty-cohort обработки вошли в diff. Provenance receipt обозначена отдельно; не надо представлять workspace TSV как независимое доказательство выполнения тестов. Но это ещё не восстановление исторически отсутствующих usage exports и не реальный benchmark скорости модели. [R11, R12]

### Два остатка, существенных для multi-repo и intervention

**Первый: cache identity всё ещё не совпадает с фактическим Azure adapter.**

`_authority_identity()` узнаёт repo-bound reader по паре `_owner` / `_repo`. Реальный `AzureRepositoryReader` хранит `_client`, `_project`, `_repo`; `_owner` и собственного `base_url` у него нет. Поэтому две Azure repositories в одном project попадают в fallback с одинаковым project ID. [R05, R06]

В source-algorithm probe оба объекта дали:

```text
("AzureRepositoryReader", "/42", "aaaaaaaa...", ".forge.yml")
```

Это не повтор дефекта repository-scoped recovery scanner: scanner можно правильно ограничить, а затем получить чужую policy из общего cache. Для заказчика с несколькими repositories это следует закрыть прежде расширения read set.

**Решение:** public `RepositoryIdentity` от adapter: connection/host/native repository ID/tenant, плюс immutable OID и config path. Нельзя строить authority key на догадке о private attributes класса. Тест должен использовать реальные constructors Azure readers с fake HTTP transport, а не удобный double с `_owner`.

**Второй: latest cancel check всё ещё находится до awaited работы publisher bridge.**

Новый wrapper читает scope и проверяет `_publication_revoked()`, затем вызывает `publish_changeset()`. Внутри bridge выполняются authoritative reads, validation и branch setup. Между проверкой и native commit остаётся время, когда pause/cancel уже может быть записан. Bridge не получает fresh grant callback/claim, который бы связывал окончательное effect authorization с этой отменой. [R04, R07]

Важно не завышать finding: предыдущая отмена *во время propose* исправлена. Оставшийся сценарий — отмена *во время publisher reads*, а не доказанный production-инцидент. Абстрактный probe иллюстрирует порядок операций, не заменяет application test.

**Решение:** готовить candidate, затем сериализовать финальное effect authorization с pause/cancel в Postgres. Разделить новые authorizations после pause и уже разрешённые/принятые remote effects. Последние нельзя считать отменёнными задним числом: они требуют correlated reconciliation и могут дать поздний superseded commit.

Также в GitHub bridge остаётся broad `GitHubAPIError → missing file` на некоторых base reads. Это нужно довести до общего typed-read контракта: 403/429/503 не доказывают отсутствие файла. В новом backlog этот участок входит в FND-03, а не раздувается в ещё один большой independent rewrite. [R07]

Полная оценка прежних D01–D12 находится в `evidence/D_CLOSEOUT.md`.

## 3. Ответ на первый вопрос заказчика

### Да: в текущем planner контекста недостаточно для обоснованного архитектурного плана

В `LLMPlanner.plan()` приходит title, description и optional path scope. Затем выполняется один JSON-mode completion через tier strong. Repository reader, search tools, shell inspection или многошаговый discovery loop отсутствуют. Лимит — 12 000 **символов**, не токенов; summary — 1 500 символов. [R02]

Builtin implementer получает больше: tree и ограниченную выборку файлов. CLI harness может исследовать workspace и делать локальный feedback loop. Но это происходит **после** согласования плана. Следовательно, нельзя обосновывать качество planning тем, что implementer потом всё равно разберётся. [R03]

### Нет: проблема не в LiteLLM

LiteLLM здесь — model gateway. Ограничение создаёт структура вызова и отсутствие tools/evidence в planning stage. Можно оставить LiteLLM для маршрутизации, budgets и observability, а сверху добавить agentic discovery. Другой вариант — использовать native harness/SDK для discovery и отдавать его проверяемый EvidenceBundle существующему planner. [X18]

Необязательно сразу переписывать planner в «универсального архитектора». Самый короткий полезный шаг — разрешённые source snapshots, поиск реализаций/ссылок, чтение contracts/migrations/tests и инструментальный результат, на который план ссылается.

### Рекомендованный planning pipeline

```text
Task
  → authorized SnapshotSet
  → read-only discovery in CI
  → code/contracts/tests evidence + questions
  → structured PlanRevision
  → deterministic evidence validation
  → human decision
```

В базовом context дать карту проекта и navigation hints. Файлы дочитывать по мере необходимости. Не запихивать все десять репозиториев в один prompt. Начать с lexical search, symbol references и явного SystemManifest; embeddings добавлять по результатам eval, а не считать обязательным компонентом. Такой подход согласуется с just-in-time context engineering, но конкретную эффективность на Forge надо измерять. [X01]

Read-only discovery не означает, что запрещено создавать scratch-файлы или запускать тесты вообще. Это означает отсутствие source-control writes и production side effects. Baseline compile/test — отдельный разрешённый probe в disposable environment. Нельзя называть arbitrary shell безопасным только потому, что Claude находится в plan mode. [X04]

## 4. Ответ на второй вопрос: план должен изменяться, полномочия — нет

В текущем `handle_issue_edited()` после gate явно сохраняется approved snapshot. Комментарий предлагает `/cancel` и `/implement` для принятия новой постановки. До approval stale plan заменяется новым run. Это правильная защита от незаметной подмены согласованной задачи, но ещё не interactive delivery. [R04]

Существующие `/retry`, repair loop и `/reconcile` тоже не эквивалентны разговору с работающим агентом. Они восстанавливают или повторяют определённые этапы. В проверенном command set нет `/pause`, `/resume`, `/steer`, `/answer` и activation новой plan revision. [R04]

Я бы разделил три объекта.

**WorkContract:** что должно получиться и что разрешено. Goal, invariants, non-goals, read/write repos/paths, budgets, credential/model routes, обязательные проверки.

**PlanRevision:** текущий способ достижения результата. Immutable version, stable step IDs, evidence refs, dependencies и preserved/invalidated work.

**ChangeProposal:** обнаруженное расхождение, доказательства, минимальное предлагаемое изменение и вопрос человеку при необходимости.

Переставить внутренние шаги, использовать найденный helper или поменять внутренний алгоритм можно в пределах заранее разрешённых tactics. Добавить writable repository, изменить публичный API/event, ввести DB migration, ослабить тесты или увеличить budget — новый material decision. Неизвестный класс изменения не должен автоматически становиться разрешённым.

Human input не требуется на каждое изменение строки. Но LLM также не должна единолично объявлять серьёзный scope expansion «маленьким рефакторингом». Machine-checkable границы проверяет policy engine; смысловые неопределённости эскалируются.

## 5. Как реализовать реальное вмешательство

Нужен durable mailbox между GitLab/GitHub/Azure комментариями и supervisor агента. Команда сохраняется с ID, sequence, actor provenance, expected revision/attempt и idempotency key. Состояния received, applied и checkpointed должны различаться.

**Pause:** сначала запрет новых publication authorizations, затем interrupt активного turn, draining старых events и checkpoint. Если tool не остановился в пределах policy, process завершается, а оператор получает точную границу восстановленного WIP. Нельзя обещать instant остановку уже принятого remote request.

**Resume:** новый epoch и совместимый execution profile; восстановление code changes, untracked files, decisions и evidence. Native session можно восстановить дополнительно, но один session ID без файлов бесполезен для воспроизводимого продолжения. [X06]

**Steer:** bounded guidance в текущем contract. Если CLI не поддерживает live input, команда применяется на следующей checkpoint boundary. Это надо честно показывать; не симулировать взаимодействие отправкой клавиш в terminal.

**Question/answer:** агент фиксирует вопрос, полезную собранную информацию и ждёт durable answer. Ожидание человека не должно держать CI runner часами. После checkpoint job можно завершить и продолжить на другом runner.

Для первого interactive adapter разумнее использовать уже знакомый Claude Code через `ClaudeSDKClient`. Официальный interface поддерживает continuous interaction и interrupt; после interrupt требуется дочитать messages предыдущего turn. Codex App Server — хороший второй adapter: `turn/steer` связан с `expectedTurnId`, а изменение sandbox/model/workspace не маскируется под steering. [X03, X07]

При этом compatibility `harness × provider/model × credential mode` нужно проверять. Рабочий current CLI с BYOK proxy не автоматически доказывает поддержку всех SDK callbacks. OpenCode server — альтернативный adapter для проверенных BYOK profiles, а не повод делать новый controller. [X08]

## 6. Что означает поддержка 10+ сервисов

Нужно явно различить три уровня.

**Multi-repo awareness:** видеть связанные producer/consumer implementations, contracts, migrations и tests. Читать несколько repositories, менять один.

**Coordinated change:** одна business task создаёт ограниченный набор изменений в двух/трёх repositories. Есть parent WorkPackage, child runs, зависимости и общий CandidateSet.

**System verification:** проверить именно выбранные версии изменённых и неизменённых сервисов с DB/broker, включая нужные mixed-version состояния.

Только третий уровень позволяет обоснованно говорить о результате системы, а не о наборе зелёных PR. И даже он не разрешает автоматический merge или production deploy.

### SystemManifest

В нём service/repository/API/event/resource identity и edges dependencies. Для первого шага достаточно административного YAML и Postgres. Backstage можно импортировать, но его owner — metadata, а не источник прав runtime. [X11]

Service graph может содержать циклы. Work-item DAG описывает конкретные этапы задачи — preparation, compatibility expansion, child implementation, integration verification. Нельзя механически топологически сортировать сервисы и называть это implementation plan.

### CandidateSet

Фиксируется набор:

```text
repository → candidate SHA
unchanged service → baseline image digest
contracts → versions/digests
test bundle → digest
environment profile → digest
```

Тестовый результат относится к этому набору. Изменился один candidate — прежний integration success не подтверждает новый набор.

Публикация across repositories — saga с `partially_published`, а не распределённая транзакция. Если PR в первом repository создан, а во втором сбой, нужно сохранить видимость partial work и восстановиться. «Откатить всё» удалением веток с human edits — неправильная компенсация.

### Databases и queues

Contract tests проверяют совместимость interfaces и сообщений. Они не заменяют реальные broker-routing/redelivery/ordering сценарии. Для них нужен focused integration environment, например с Testcontainers, но container orchestration должен оставаться у trusted test executor, без host Docker socket в coding-agent shell. [X12–X14]

DB migration надо проверить от baseline schema с synthetic data, не только на пустой БД. Для mixed-version rollout полезен expand/migrate/contract. Порядок merge/deploy и ограничения rollback — отдельный human-facing result, не полномочие бота. [X15]

## 7. Почему не предлагаю немедленный переход на Temporal/LangGraph

Эти инструменты дают полезные primitives, но не исправляют за Forge scope, credentials, source snapshots и publication reconciliation. LangGraph pause/resume также имеет replay semantics; Temporal Signals/Updates дают хороший образец durable interaction. [X09, X10]

У тебя уже вложена работа в Postgres state, leases, intents и tests. Сейчас дополнительный authoritative engine скорее создаст второй источник истины. Оставить текущий controller, добавить нормальные domain objects и message semantics — более экономный первый путь.

Если позже измерения покажут, что объём долгоживущих workflows, timers и эксплуатационная нагрузка оправдывают Temporal, это отдельное migration decision. Не prerequisite для customer pilot.

## 8. Практический порядок работ

**P0 — foundation.** Закрыть реальные adapter cache identity и окончательное publication fencing, добавить production-path regressions. Read-only discovery prototype можно вести параллельно: он не требует новых write privileges.

**P1 — хороший план, основанный на коде.** Один provider/profile, source snapshot, discovery tools, EvidenceBundle, PlanRevision, human questions. Сравнить с нынешним issue-only planner на одинаковых задачах. Не ждать универсального graph/RAG layer.

**P2 — управляемая single-repo реализация.** WorkContract отдельно от plan, ChangeProposal, ревизии, pause/resume/steer и portable WIP checkpoint. Один interactive adapter сначала; остальные через capability-aware fallback.

**P3 — multi-repo.** Разрешённое чтение десяти repositories, один writer, затем parent package с двумя/тремя writer repos. Compatibility plan, CandidateSet и integration gate.

**P4 — representative pilot.** Десять synthetic/реальных согласованных сервисов, заранее зафиксированные tasks и acceptance checks, faults, human replan и complete spend/evidence. Дополнительные adapters и runtime recipes включаются только по наблюдаемой необходимости.

64 backlog items распределены по восьми epics: foundation, discovery, planning, human control, runtime, multi-repo, verification, operations. P1 означает prerequisite **для соответствующего capability**, а не 64 блокирующие ошибки текущего release. В backlog есть dependencies и относительные размеры, но нет выдуманного календарного обещания.

## 9. Измерение качества и производительности

Для планов: resolvable citations, поиск затронутых dependencies, выполнимость шагов, выявление missing decisions и число существенных human corrections. Подробный план с выдуманными путями хуже короткого с честным вопросом.

Для исполнения: accepted outcome, all-attempt spend, human rework, сохранённый WIP после interruption, correct recovery и доля false-ready в фиксированном тестовом наборе.

Для latency: TTFT, full model request, tool execution, CI queue, human wait, command-to-applied и total lead time. Это разные величины. Logical input/cache traffic нельзя делить на output decode throughput.

Для multi-repo budgets: parent reservation и child allowances, учёт discovery/planning/implementation/tests/review. Cancel, pause или новая plan revision не обнуляют уже потраченные и unresolved amounts. Если native CLI даёт aggregate-only counters, уровень accounting и enforcement остаётся partial.

Новый measured tokens/s в этом review не получен. Прежние migration traffic estimates можно использовать как нагрузочный сценарий, но не как реальную скорость модели или цену нового workflow.

## 10. Вариант ответа заказчику

> Спасибо, ты правильно выделил два ограничения текущей реализации.
>
> Сейчас planner действительно устроен проще, чем coding-agent execution: он получает постановку и ограничения, но не делает полноценное инструментальное исследование repositories. Я не считаю это достаточной конечной моделью для сложных изменений. Следующий шаг — отдельный discovery stage: агент читает реальные исходники, contracts, migrations и tests, формирует план со ссылками на evidence и задаёт вопросы там, где данных недостаточно. LiteLLM при этом можно оставить gateway — дело не в нём, а в самом planning workflow.
>
> С изменением плана согласен. Сейчас Forge защищает approved snapshot: после approval изменение issue не подмешивается в работающий run. Это полезная гарантия, но для нормальной работы нужно добавить управляемые revisions. Внутренние тактические изменения агент сможет делать в согласованных границах. Новые repositories, изменение public contracts, schema migrations и другие существенные изменения будут оформляться отдельным proposal с сохранённым прогрессом и понятным решением человека. Pause, уточнения и resume должны работать без потери выполненной работы.
>
> Поэтому я не ограничиваю идею маленькими проектами. Но и обещать, что сегодняшняя версия автономно переделает систему из десяти сервисов, было бы неправильно. Я разделяю чтение нескольких repositories, согласованное изменение нескольких сервисов и интеграционную проверку конкретного набора версий. Начать предлагаю с одного реального сквозного сценария: увидеть зависимости всей системы, менять ограниченный набор repositories и проверять результат с нужными DB/queue dependencies. Это даст измеряемый ответ, где Forge уже экономит работу, а где ещё требует доработки.

Это draft ответа, а не сообщение, отправленное заказчику. Он специально отделяет текущие возможности от roadmap.

## 11. Итоговая оценка

Текущая реализация стала лучше как контролируемый execution layer. Но заказчик спрашивает о следующем уровне — качестве исследования, адаптации и результате системы. Его нельзя получить только закрытием очередных низкоуровневых findings.

Правильный следующий шаг: **не замораживать каждую тактическую мысль, а замораживать полномочия; не давать модели всю организацию, а давать проверяемый контекст; не считать набор PR результатом системы, а проверять CandidateSet**.

Полные предложенные contracts, transaction semantics, framework tradeoffs, failure cases, staged delivery и 64 implementation stories — в английских документах этого комплекта. Все новые commands и interfaces там помечены как proposed.
