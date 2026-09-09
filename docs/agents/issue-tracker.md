# Issue tracker: Local Markdown

Issues and specs for this repository live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation tickets are one file each at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`
- A ticket records its state in a `Status:` line; new tickets use `ready-for-agent`
- Comments and conversation history append under `## Comments`

## Publishing and retrieval

When an engineering skill says "publish to the issue tracker", create the appropriate file beneath `.scratch/<feature-slug>/`. When a user references a ticket path, read that file as the tracker record.
