# Phase 2.1 reply-directionality repair

Apply over Phase 2.

Changes:
- fixes mypy narrowing for asyncio.gather(return_exceptions=True)
- persists Discourse topic author in source_state
- non-topic-author community replies cannot become buyer alerts
- marketplace non-author replies classify as SERVICE_PROVIDER
- adds `python -m lead_radar reclassify --source make --source n8n`
- reclassification updates stored commercial scores only and sends no Telegram alerts
- adds regression tests for applicant replies and author-reply persistence
