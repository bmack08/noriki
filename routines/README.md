# Scheduled agents

Two recurring cloud agents that keep Noriki and OVERSEER improving without
Brandon having to think about it. Both were specced on 2026-08-16 but **could
not be created yet** — cloud routines clone from GitHub, and `noriki` /
`overseer` did not exist as repos at the time (the API returned 403).

Once `setup-github.ps1` has run, these can be created in one step.

Both run on **Opus 5**, weekly, staggered so two PRs never land the same day.
If it becomes noise, drop the second one to bi-weekly — cron
`0 13 8-14,22-28 * 4` fires it on the second and fourth Thursday.

---

## 1. Weekly systems & agent research

**When:** Mondays, 09:00 America/New_York → cron `0 13 * * 1` (UTC)
**Repos:** `noriki`, `overseer`
**Tools:** Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch

Researches how the *system* — workflows, orchestration, agent design — can be
better. Explicitly not a feature-builder. Ranks proposals by **how much founder
attention each one saves per week**, which is the only metric that matters here.

Output: `research/YYYY-MM-DD-systems.md` plus a PR whose description is five
phone-readable lines. Bugs found along the way are fixed in the same PR without
asking.

## 2. Weekly functional & design review

**When:** Thursdays, 09:00 America/New_York → cron `0 13 * * 4` (UTC)
**Repos:** `noriki`
**Tools:** Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch

Two questions, every week: what can be better *functionally*, and what can be
better *structurally* — CSS, UX, UI, ergonomics, accessibility. Leans toward
invention rather than tidying, because the brief was "I don't want to spend time
racking my brain on fixing things — focus on innovative ideas."

Applies clear fixes directly. Proposes ideas separately, as ideas.

---

## The standing rule both agents follow

> Fix what you know is broken. Bring ideas, not permission requests. Escalate
> only genuinely critical calls — anything spending money, anything hard to
> reverse, anything outward-facing, anything touching a frozen region.

A finding that lengthens the manual lane is a regression, however clever.
