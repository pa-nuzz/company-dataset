# Engineering Constraints & Workflows
- Always verify Python code changes by running `pytest` before completing a task.
- When modifying database logic, work on `store.db` with atomic transactions.
- Keep code updates aligned with clean module structure (Ingest -> Identity -> Enrich -> Export).