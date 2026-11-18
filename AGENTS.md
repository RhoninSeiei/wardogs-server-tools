# AI workflow · AI 工具工作流

Read README.md, docs/OPERATIONS.md and service/openapi.json. service/app.py exposes management endpoints. event_receiver/receiver.py ingests events and event_receiver/local_cache.py synchronizes the archive.

Start from read-only health, aggregate state, cursor freshness and operation receipts. Use synthetic RCON responses and temporary databases for tests. Limit each client to the required token and scope. Exclude live credentials, raw chat and player data from model prompts.

真实处罚、消息、配置写入和平台条款确认由操作者授权。超时后核对原操作号，保留审计并避免重复执行。归档后如需继续开发，请使用自己的派生仓库。
