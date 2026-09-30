# Security behaviour

Operator-visible security behaviour of this fork. Settings live in
`OpenSite/settings.py`; the helpers in `OpenBench/security/`.

## Failed-login throttle

Every password check that passes its request through Django's authentication
goes through `ThrottledModelBackend`: the website login, `/scripts/`, the
Client's `/clientWorkerInfo/` and `/clientGetNetwork/`, credentialed `/api/`
calls (including the supervisor's `POST /api/active/`), and `/admin/login/`.

- After 10 failed checks within 15 minutes for one username, or from one client
  address, further checks for that key fail without testing the password,
  even when it is correct. The window runs from the first failure; it is not
  extended by later ones. Successful logins do not count.
- A worker or supervisor with correct credentials never fails, so it is never
  throttled in normal operation. It is locked out only while someone else keeps
  failing for its username or from its address; it recovers by itself when
  the window ends. Restarting the container clears every counter.
- The client address is `REMOTE_ADDR`, or, when `OPENBENCH_BEHIND_TLS_PROXY` is
  set, the right-most `X-Forwarded-For` entry, which is the one the proxy
  appended. Never set that flag when clients can reach gunicorn directly.
- Counters live in Django's per-process local-memory cache. Each gunicorn
  process counts on its own, so the effective limit is up to 10 ×
  `OPENBENCH_WORKERS` failures per window. A shared cache would be needed for
  an exact limit.
- Each failed check logs one line on the `OpenBench.views` logger:
  `Authentication failed for username '<name>' from <address> on <path>`. The
  password is never logged.

## Sessions and state-changing requests

- Logout is `POST /logout/` with a CSRF token; the sidebar link submits that
  form. `GET /logout/` only redirects to the index and leaves the session alone.
- `/register/` refuses both GET and POST while `require_manual_registration` is
  set.
- Workload actions (`/test/<id>/APPROVE/` and friends) and the Network
  `DEFAULT` and `DELETE` links are plain GET links. They, and
  `POST /api/networks/<engine>/<name>/delete/`, are refused when the browser
  reports `Sec-Fetch-Site` as `cross-site` or `same-site`, so another site,
  including another subdomain, cannot trigger them through a logged-in browser.
  Scripts and the Client send no such header and are unaffected.
- `/scripts/` is exempt from CSRF, so it acts only as the user named by the
  `username` and `password` in its POST body, which must be enabled.
- Only Approvers may delete Networks through the API, as on the website.

## Workers

A Machine may only report into its own Results: `/clientSubmitResults/`,
`/clientSubmitNPSStats/`, `/clientSubmitPGN/`, `/clientBenchError/` and
`/clientSubmitError/` require that the Machine was assigned the Test (and, where
given, owns the Result). Results with negative counts are refused.
`/clientSubmitResults/` answers such a report with `{ "stop" : true }`; the
others answer `{ "error" : ... }`, which makes the Client restart its session.
A genuine Client never triggers either.

## Response headers and cookies

Django sets `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`,
`Cross-Origin-Opener-Policy: same-origin` and `X-Content-Type-Options: nosniff`.
HSTS is left to the TLS proxy. There is no Content-Security-Policy yet, since
the Templates rely on inline scripts. Session and CSRF cookies are `HttpOnly`
and `SameSite=Lax`, and sessions last 7 days.
