# artlist-farm

Pipeline to run image generations on artlist toolkit through their free tier.
Workers run on GitHub Actions to avoid local IP rate-limits.

## Layout

- `src/` — core client (signup, generation flow)
- `scripts/test_signup.py` — minimal smoke test: signup + verify session
- `scripts/run_one.py` — full text-to-image run from a single account
- `.github/workflows/test-signup.yml` — manual workflow to test if GitHub runner IP passes Cloudflare/anti-abuse on signup

## Usage

1. Push this dir to a (private) repo
2. Open Actions → "test signup" → Run workflow
3. Check logs: does signup return HTTP 200?
   - 200 → GitHub IP range works, ready to scale via matrix-jobs
   - 401/403 → need different routing (residential proxy or VPS with IPv6 rotation)
