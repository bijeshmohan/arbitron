# Credential rotation

Rotate on a schedule (at least every 90 days for exchange keys), when someone
with access leaves, and **immediately** if a leak is suspected. In that case
trip the kill switch first ([kill-switch](kill-switch.md)).

| Secret | Where | Rotate |
|--------|-------|--------|
| API token | `deploy/secrets/arbitron_api_token` | `openssl rand -hex 32 > deploy/secrets/arbitron_api_token`; `$C up -d --force-recreate app prometheus` |
| Telegram bot token | `deploy/secrets/arbitron_telegram_bot_token` | revoke with @BotFather (`/revoke`), write the new token, `$C up -d --force-recreate app` |
| Database passwords | `deploy/secrets/*_password`, `*_database_url` | `docs/deployment.md` → Rotating secrets |
| Exchange API key (sandbox now, live later) | `ARBITRON_EXCHANGE_API_KEY` / `_SECRET` | below |

**Exchange keys (operator only).** The bot never creates or changes keys.

1. Trip the kill switch.
2. On the exchange, create the new key on the **dedicated sub-account**:
   - Permissions: **spot trading only**. Withdrawals, margin, futures and
     universal transfer stay disabled.
   - Restrict access to the host's IP address.
   - Testnet keys can't be restricted. That's acceptable only because
     testnet funds are virtual.
3. Install it: replace the value where it's injected (the local `.env` for
   sandbox, or the secret file for a future live deployment), then recreate
   the app.
4. Check it before deleting the old key. In sandbox,
   `arbitron sandbox check <config>` must pass. In live, a balance read must
   work and the exchange's key page must show no withdrawal permission.
5. **Delete the old key on the exchange.** Rotation isn't finished until the
   old key is gone.
6. Review and re-arm the kill switch.

**Verify:** the old key returns an authentication error if tried, the new
key passes the check, and `GET /ready` returns 200.

**Never:** paste keys into chat, tickets or commits; give a key
withdrawal permission; or reuse a key across paper, sandbox and live.
