# Domain Docs

How engineering skills should consume this repository's domain documentation.

## Before exploring

- Read `CONTEXT.md` at the repository root when it exists.
- Read ADRs in `docs/adr/` that concern the area being changed.
- If these files do not exist, proceed silently. Do not create them preemptively.

## Layout

This is a single-context repository. Domain vocabulary belongs in root-level `CONTEXT.md`; architectural decisions belong in `docs/adr/`.

## Use domain vocabulary

Use terms defined in `CONTEXT.md` for issue titles, design proposals, hypotheses, and test names. If a needed concept is absent, treat that as either a reason to reconsider the terminology or a candidate for later domain modeling.

## Flag ADR conflicts

If a proposed change conflicts with an ADR, surface the conflict explicitly rather than silently overriding it.
