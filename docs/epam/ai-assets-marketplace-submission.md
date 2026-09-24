# EPAM AI Assets Marketplace — план сабмита forge

> Изучено 2026-09-24 по инструкции EPM-EASE (конструктор каталога, форматы
> Anthropic skill/agent, Snyk Agent Scan). Рабочий документ: что подаём,
> что для этого нужно, черновики артефактов, чеклист.

## 1. Жёсткие требования (гейты сабмита)

1. **Репозиторий на EPAM-hosted Git** — `git.epam.com` / `eu.git.epam.com` /
   `gitbud.epam.com` / `gitpct.epam.com` (или другой инстанс EPAM).
   **GitHub не подходит** — краулер туда не дотянется. Нужен зеркальный/
   экспозиционный репозиторий внутри EPAM.
2. **Public for EPAM** — видимость «внутри EPAM», не private-by-team;
   иначе сабмит молча не опубликуется.
3. **Структура папок**: `skills/<name>/`, `agents/<name>/`,
   `factories/<name>/`. Имя папки = ID актива: только буквы/цифры/дефисы/
   подчёркивания (точки и пробелы ломают discovery).
4. **Frontmatter** (YAML между `---`): `name`, `description`, `author`
   (с email `@epam.com`). Для factory — файл ровно `FACTORY.md`
   (чувствительно к регистру), `support_level` ∈ {Self-Serve,
   Best Effort Support, Dedicated Capacity}, `sdlc_phase` — одно значение
   (не список), значения с двоеточиями — в кавычках.
5. **Snyk Agent Scan** — периодическое сканирование; High/Critical = 5
   рабочих дней на фикс, иначе карантин (после фикса возвращается сам).

## 2. Что подаём из forge (маппинг на их таксономию)

Forge целиком = **factory** в их определении (cross-role, cross-phase:
issue → researched plan → approval → implement → independent verify →
Draft MR; человек мержит). Подаём один factory + два skills; агентов —
нет (наши drivers — runtime-компоненты, не Anthropic-формат; честнее
не разменивать тип).

| Актив | Папка | Что это |
|---|---|---|
| Factory | `factories/forge-agentic-delivery/` | Весь сквозной workflow: `pip install forge==<ver>` (wheel с GitHub Releases, public) + onboarding-шаблоны; операции /implement /go /pause /steer /retry /resume |
| Skill 1 | `skills/forge-wip-continuation/` | Дисциплина checkpoint/restore/steering: когда WIP переживает runner loss, exact-resume, ротация generation-токенов — переносимая экспертиза для ЛЮБОГО agentic-тула |
| Skill 2 | `skills/forge-independent-verification/` | Независимая верификация кандидата: oracle-тесты, отчёты по обязательствам (report inventory), fresh-vs-stale evidence — тоже переносимо |

Проверка на «production-tested» (их главное условие): forge имеет
executed evidence — live single-writer прогон (issue→модель→Draft MR,
oracle green), 12-задачный lab-пилот, deployment-ops 5/5 — всё в
`docs/evaluation/`, `qualification/`. В FACTORY.md честно указываем
уровень: live-proven на GitLab CE-профиле.

## 3. Что нужно сделать (порядок)

1. **Создать репо на git.epam.com** — например
   `git.epam.com/<group>/forge-assets`. НЕ зеркало всего кода: маленький
   экспозиционный репо с `factories/` + `skills/` + README + ссылками на
   публичный GitHub (wheel/releases) — так соблюдаются и BSD-провенанс
   (происхождение только в LICENSE/UPSTREAM.md), и Snyk-скан получает
   маленькую чистую поверхность.
2. **Видимость** — Internal/Public for EPAM; проверить краулероспособность
   (открыть в инкогнито с EPAM-сессией).
3. **Написать артефакты** (черновики ниже), прогнать
   `python -c "import yaml,sys; yaml.safe_load(open(f).read().split('---')[1])"`
   на каждом frontmatter.
4. **Self-scan** — Snyk Agent Scan (agent-scan-launcher) по своему репо
   ДО сабмита; почистить High/Critical (ожидаемых нет: секретов в репо
   нет — всё env-based; в скриптах нет `curl | bash`).
5. **Проверить установку изнутри EPAM-сети**: `pip install
   forge==0.37.0` тянет wheel с GitHub Releases (репо public) — ок;
   GHCR-образ тоже public. Если у практики есть внутренний PyPI-mirror —
   продублировать wheel туда для надёжности.
6. **Сабмит** — письмо на SpecialEPMFeedback@epam.com по их шаблону
   (один URL на тип актива; см. §6).

## 4. Черновики артефактов

### factories/forge-agentic-delivery/FACTORY.md

```markdown
---
name: Forge — Agentic Delivery Factory
description: "Issue-to-MR agentic factory: researched plan, human approval, implementation lane, independent verification, Draft MR (human merges)."
owner: Forcewake / EPAM practice TBD
authors:
  - "Pavel Nasovich <pavel_nasovich@epam.com>"
install_script: "pip install forge==0.37.0 && python -m forge.doctor"
install_script_unix: "pip install forge==0.37.0 && python -m forge.doctor"
sdlc_phase: Accelerated Feature Development
support_level: Self-Serve
project_deployments:
  - ND
use_cases:
  - Agentic implementation of backlog issues with independent verification
  - Cross-runner WIP continuation (pause, steer, exact-resume)
  - Draft-MR delivery with human merge decision
---

# Forge — Agentic Delivery Factory

Forge turns a GitLab CE / GitHub / Azure DevOps issue into a verified
Draft MR: a researched plan (multi-repo discovery with citations) →
explicit human approval → an implementation lane (Claude Code / Codex /
OpenCode / Copilot harnesses) → independent verification (oracle tests
+ required report inventory) → Draft MR. The bot never merges.

## What's proven
- Live single-writer flow on GitLab CE (real model, oracle-green
  candidate, cross-runner exact-resume) — evidence in the repo's
  `docs/evaluation/` and `qualification/` directories.
- Operator controls: /implement /go /pause /steer /retry /resume with
  generation-scoped credentials.

## Install
pip install forge==0.37.0 (wheel + cosigned image on GitHub Releases /
GHCR, public). Run `forge-doctor` for the preflight; onboarding
templates for the target repo are generated by the control plane.

## Source
https://github.com/forcewake/forge (BSD-2; provenance in LICENSE /
UPSTREAM.md).
```

### skills/forge-wip-continuation/SKILL.md

```markdown
---
name: forge-wip-continuation
description: Checkpoint/restore discipline for agentic coding sessions — when WIP survives interruption, exact-resume semantics, generation-scoped control credentials, and how to verify restoration actually happened.
author: Pavel Nasovich <pavel_nasovich@epam.com>
---

# WIP continuation for agentic coding tools

... (содержание: чекпоинт перед прерыванием; exact-resume по digest,
не «последний»; восстановление ≠ ACK команды; идентичность attempt;
ротация токенов по generation; collector из активной generation)
```

### skills/forge-independent-verification/SKILL.md

```markdown
---
name: forge-independent-verification
description: Independent verification of AI-produced candidates — oracle tests, required report inventories with per-obligation identity, freshness/staleness of evidence, and why a green harness job is not acceptance.
author: Pavel Nasovich <pavel_nasovich@epam.com>
---

# Independent verification discipline

... (содержание: verification ≠ PR created; report inventory per
obligation; attempt ordering; exit codes; stale evidence never decorates
a new candidate)
```

## 5. Риски/замечания

- **Провенанс**: forge — независимый проект; в EPAM-репо НЕ упоминаем
  «based on Codeward» — только LICENSE/UPSTREAM.md (директива от
  2026-09-16).
- **Email в author** — нужен EPAM-адрес Павла (в черновиках заглушка —
  заменить).
- **owner** — заполнить реальной практикой/группой EPAM.
- **install_script**: console_scripts в wheel НЕТ (проверено по
  артефакту v0.37.0 — entry_points.txt отсутствует) — поэтому
  `python -m forge.doctor`; добавить `forge-doctor` как entry-point —
  мелкий upstream-фикс к следующему релизу.
- `project_deployments: []` не оставлять пустым явно — либо убрать,
  либо `- ND` (мы поставили ND как строку; по их правилам пустой список
  = ND-sentinel, строка ND допустима как plain string).

## 6. Письмо-сабмит (шаблон)

```
To: SpecialEPM-EASEFeedback@epam.com
Subject: New AI Artifact Submission

factories: https://git.epam.com/<group>/forge-assets/-/tree/main/factories
skills: https://git.epam.com/<group>/forge-assets/-/tree/main/skills
owner: <практика/группа>
```

Агентов не подаём (пустую строку agents: не указываем).
