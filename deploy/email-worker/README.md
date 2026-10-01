# Email in (Cloudflare Email Routing)

Mail sent to `<token>@in.flowly.net` becomes a task in the workspace whose Email integration has that token.
Cloudflare receives the mail and this worker posts it to the Flowly API.

1. **Shared key.** In Flowly, Admin → Settings → Email in: set *Inbound domain* to `in.flowly.net` and
   *Inbound key* to a long random string.
2. **Worker.** Here: `npm install`, `npx wrangler login`, `npx wrangler secret put FLOWLY_KEY` (the same key), then
   `npm run deploy`.
3. **Email Routing.** Cloudflare dashboard → flowly.net → Email → Email Routing:
   - enable it for the subdomain `in.flowly.net`. Cloudflare adds the MX and SPF records; leave them as they are.
   - Routing rules → *Catch-all address* → action **Send to a Worker** → `flowly-email-in`.
4. **Test.** Add an Email integration to a workspace (Integrations → Email in), copy its address and send it a
   message from your account's email. A task appears; the delivery log on the integration shows what happened.

Unknown addresses are rejected. If Flowly is down, the error makes the sender's server try again later.
