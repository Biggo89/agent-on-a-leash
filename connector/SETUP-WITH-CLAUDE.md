# Set up Leash Wallet with Claude

Leash Wallet is the connector that puts a Claude agent on the leash. The agent can propose
a mandate, ask what is allowed, and request payments. Only the cardholder, on the phone, can
confirm, answer, or revoke. Every command, with its output and what to do when it fails,
is in [`../wallet-control-layer/PLAYBOOK.md`](../wallet-control-layer/PLAYBOOK.md) Part 10.

## 1. Start it

From `wallet-control-layer/`, in two terminals:

```bash
make sandbox
```

```bash
LEASH_COMPILER=baseline make connect
```

`make connect` prints the two URLs you need:

- MCP: `http://127.0.0.1:8010/mcp`
- Phone: `http://127.0.0.1:8010/app/` (keep it open in a browser)

## 2a. Claude Code

```bash
claude mcp add --transport http Leash-Wallet http://127.0.0.1:8010/mcp
```

```bash
claude
```

In Claude Code: `/mcp` → **Leash-Wallet** → **Authenticate**. The browser opens the LEASH
onboarding. Sign in as any persona (any PIN), go through the steps, and tap **Confirm Leash**.
The terminal then says *Connected to Leash-Wallet*.

Claude Code allows no spaces in a server name, so it is `Leash-Wallet` here. Everywhere else
it is **Leash Wallet**.

## 2b. claude.ai / Claude Desktop

These run in the cloud, so the laptop needs a public URL. One tunnel covers everything:

```bash
cloudflared tunnel --url http://localhost:8010
```

In claude.ai: **Customize → Connectors → Add → Add custom connector**.

1. Name: **Leash Wallet**
2. URL: `https://<name>.trycloudflare.com/mcp`
3. Tap **Add**, then **Connect**. You get the LEASH onboarding; tap **Confirm Leash**.

Enable Leash Wallet in the chat's connector menu. The phone is at
`https://<name>.trycloudflare.com/app/`.

## 3. Use it

In the chat:

1. *What am I allowed to buy with my card? Ask Leash Wallet.*
2. *Here is my instruction for Leash Wallet, pass it on word for word:* followed by the
   cardholder's sentence. Then, on the phone: **Open decisions → Confirm**.
3. *Order a weekly grocery basket for about CHF 70, delivered.* The answer is approved,
   declined with the reason, or pending. If it is pending, answer it on the phone, then ask
   Claude to check the status.

To cut the agent off: on the phone, **Manage Leash → Revoke**.

## Remove it

```bash
claude mcp remove Leash-Wallet -s local
```

```bash
rm -f out/payment-app.json out/connector.json
```

Run the second command in `wallet-control-layer/`. It makes the app forget every agent,
consent and proposal. In claude.ai, remove the connector under **Customize → Connectors**.
