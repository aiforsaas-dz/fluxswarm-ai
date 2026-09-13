# FluxSwarm · Paddle dashboard setup (env vars — names only, no secrets)

> This file lists the **exact environment variables** the backend reads for Paddle
> (`main.py:2795, 2805-2806, 2811, 2946`) and where each value lives on the
> Paddle dashboard. It deliberately contains **no secret values** — keys belong in
> your Render dashboard, never in git or in any chat log.

## 1. Create your Paddle vendor account (once)
1. Sign up at **paddle.com** (they are the merchant of record).

## 2. Env vars to add on Render → your service → Environment
Add ALL of these (exact names, no quotes):

| Var name (exactly as code reads it) | Value source on Paddle Dashboard | Required? |
|---|---|---|
| `PADDLE_CLIENT_TOKEN` | Settings → Checkout → Client-side token | **Yes — without it the checkout page shows a "not ready" message (main.py:2811).** |
| `PADDLE_API_BASE` | Must contain `sandbox` for the sandbox environment; use the production API URL otherwise (main.py:2805-2806 uses it to pick the environment). | Yes |
| `PADDLE_WEBHOOK_SECRET` | Settings → Developer tools → Webhooks → your endpoint's signing secret | Only if you use webhooks. The endpoint is `/api/payments/webhook`. |
| (optional) `PADDLE_API_BASE` override for BYOK note | — | — |

## 3. Webhook (optional but recommended)
Add webhook URL: `<your-domain>/api/payments/webhook` and set `PADDLE_WEBHOOK_SECRET` to its signing secret.

## 4. After saving env vars on Render
Render restarts the service automatically with the new env. Then verify:
- `GET /checkout` shows the checkout page (not the "Paddle is not ready" fallback).
- The trust board on `/` and `/trust` continues to show the **live measured** checks count (it never depends on these keys).

## 5. What this file is NOT
- Not a license key file.
- Not the actual credentials — you are the only one who ever sees them.
