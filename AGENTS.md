# Project progress maintenance

The user requires a local, current checklist for the overall architecture and
Harness Engineering work.

- Read `docs/架构改造进度.md` and the relevant section of `docs/TASKS.md` before
  continuing this work. These files are the progress entry point and execution
  record; detailed requirements remain in the linked architecture/domain docs.
- Before committing each implementation batch, update both files with delivered
  scope, remaining work, next priority, verification evidence and its limits.
  Update affected design documents when behavior or contracts change.
- Keep partial work unchecked. Separate code delivery, related regression,
  frozen-source full regression, performance and real-environment acceptance.
  Do not carry historical validation forward as evidence for changed source or
  derive completion percentages by counting checklist rows/tests.
- Record newly found gaps and explicit deferrals. Preserve stable work-package
  IDs when splitting tasks and explain any change in scope.
- In a subsequent progress update, record the actual business commit hash once
  it exists. Documentation-only commits do not replace that business baseline.
