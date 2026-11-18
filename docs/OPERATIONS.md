# Server operations · 服务运维

1. Start the API with the README settings. `API_HOST` defaults to loopback. Use a private network or tunnel for game RCON, whose transport is HTTP.
2. Generate independent files for queries, report execution and admins. Enable the latter only as needed with `MODERATION_API_KEY_FILE` and `ADMIN_MODERATION_API_KEY_FILE`. `ADMIN_MODERATION_SCOPES_FILE` contains `{"admin_moderation_scopes":[{"platform":"qq_official:instance","botId":"10001","groupId":"example-openid"}]}`. Use the actual plugin actor tuple.
3. Run `python -m event_receiver.receiver` with distinct `EVENT_TOKEN_FILE` and `EVENT_QUERY_TOKEN_FILE`, a private `EVENT_DB` and a free `API_PORT`. Put it behind your TLS proxy. Configure game ingestion at `POST /wardogs`. The cache reads `GET /wardogs/v1/events` using the query token.
4. Run `python -m event_receiver.local_cache` with `EVENT_EXPORT_HOST` (receiver TLS hostname), optional `EVENT_EXPORT_ORIGIN`, `RCON_HOSTPORT`, `RCON_KEY_FILE`, `EVENT_QUERY_TOKEN_FILE` and a different `API_PORT`. Create `data/local-cache` and set the cache's `EVENT_DB=data/local-cache/events.sqlite3` when the management DB is `data/state.db`.
5. Verify authenticated `GET /v1/sync-status` on the cache. Cursor equality measures synchronization with the receiver. Check game event freshness independently.
6. 接收端会存储上游发送的事件，其中可能包括聊天。按部署需求限制上报字段与数据库访问。缓存默认不主动轮询 RCON 聊天，`ENABLE_RCON_CHAT_SYNC=1` 才开启。

Run `python -m unittest discover -s tests` with loopback sockets available. Tests use temporary SQLite databases and fake RCON. Back up SQLite with its backup API. Preserve operation IDs and reconcile uncertain outcomes before retrying.

回溯为空时依次核对游戏推送、接收批次、同步游标和时间窗。停止运营时先关闭写入入口，再备份配置、事件与业务审计。
