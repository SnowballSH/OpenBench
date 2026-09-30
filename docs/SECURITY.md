# Security behaviour

Operator-visible security behaviour of this fork. Settings live in
`OpenSite/settings.py`; the helpers in `OpenBench/security/`.

## Failed-login throttle

Every password check is throttled: the website login, `/scripts/`, the
Client's `/clientWorkerInfo/` and `/clientGetNetwork/`, credentialed `/api/`
calls (including the supervisor's `POST /api/active/` and the Client's network
downloads from `/api/networks/`), and `/admin/login/`. The views check the
throttle before testing a password; `LoginThrottleBackend` does the same for
the admin login, and a `user_login_failed` receiver counts the failures.

- Two counters, each over a 15-minute window fixed from its first failure:
  - one per username and client address pair, refused after 10 failures;
  - one per client address, refused after 50 failures.
- No counter is keyed on the username alone. Failures from one address never
  lock that account out anywhere else, so an outsider cannot lock out
  `lab-worker` or `lab-readonly` without sharing their address.
- A refused check does not test the password and is not counted again. A
  worker retrying a stale password therefore locks only its own username on
  its own address, and never grows the address counter past its own 10.
- A refused check answers:

  | Where | Answer |
  |---|---|
  | `/login/`, `/scripts/` | Redirect to `/login/` with "Too many failed logins. Try again later" |
  | `/clientWorkerInfo/` | `{"error": "Too many failed logins. Try again later"}`. The Client treats it like any other error: it sleeps and registers again. |
  | `/clientGetNetwork/` | 429, plain text |
  | `/api/*` | 429 `{"error": "Too many failed logins"}` |
  | `/admin/login/` | The admin's usual invalid-login form |

  The Client writes whatever `/api/networks/` returns into the network file,
  so a throttled worker fails that download's SHA check (exit 4 under
  `--single-workload`). That only happens once its own credentials have
  failed 10 times from its address, or 50 checks failed from that address.
- The client address is `REMOTE_ADDR`, or, when `OPENBENCH_BEHIND_TLS_PROXY` is
  set, the right-most `X-Forwarded-For` entry, which is the one the proxy
  appended. Never set that flag when clients can reach gunicorn directly.
- Counters live in Django's per-process local-memory cache (up to 10,000
  entries). Each gunicorn process counts on its own, so the effective limits
  are up to `OPENBENCH_WORKERS` times higher. Restarting the container clears
  every counter.
- Each failed or refused check logs one line on the `OpenBench.views` logger,
  with the username and path quoted by `repr`, so a crafted value cannot forge
  log lines. The password is never logged.
- `django.contrib.auth.backends.ModelBackend` still checks every password and
  stays in `AUTHENTICATION_BACKENDS`, so browser sessions from before this
  change stay logged in.

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
