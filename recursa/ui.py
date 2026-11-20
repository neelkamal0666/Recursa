"""Local review dashboard (``recursa ui``). Binds to 127.0.0.1 by default.

Reviewers sign in with the same passphrase as the CLI; the session cookie is a
short-lived signed token (HttpOnly, SameSite=Strict) and every form carries a CSRF
token derived from it. Approvals submit the packet hash shown on the page, so an
approval binds to exactly what the reviewer saw.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs

from .auth import Auth, AuthError
from .cli import render_packet

CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--accent:#0f766e;--bad:#b91c1c;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;
--accent:#2dd4bf;--bad:#f87171;--card:#292524}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:980px;
padding:16px}
a{color:var(--accent)}pre{background:var(--card);border:1px solid var(--line);padding:12px;overflow:auto;
white-space:pre-wrap;font-size:13px}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid
var(--line);padding:6px 4px;text-align:left;font-size:14px}.muted{color:var(--muted)}button{padding:6px 14px;
margin-right:8px}textarea,input{width:100%;box-sizing:border-box;padding:6px;margin:4px 0 10px;
background:var(--card);color:var(--fg);border:1px solid var(--line)}.err{color:var(--bad)}
"""


def serve(learner, host: str = "127.0.0.1", port: int = 8765) -> None:
    auth = Auth(learner.paths.auth)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # quiet
            pass

        # -- helpers ----------------------------------------------------------------
        def _token(self) -> str | None:
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return c["recursa"].value if "recursa" in c else None

        def _who(self) -> str | None:
            try:
                return auth.verify_token(self._token())["sub"]
            except AuthError:
                return None

        def _csrf(self) -> str:
            return hmac.new(auth._secret(), (self._token() or "").encode(), hashlib.sha256).hexdigest()[:32]

        def _form(self) -> dict[str, str]:
            n = int(self.headers.get("Content-Length", 0))
            return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}

        def _page(self, title: str, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
            who = self._who()
            nav = (f'<p class="muted"><a href="/">Review queue</a> · signed in as {html.escape(who)} '
                   f'<form method="post" action="/logout" style="display:inline"><button>Sign out</button></form></p>'
                   if who else "")
            doc = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
                   f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
                   f"<style>{CSS}</style></head><body><h1>{html.escape(title)}</h1>{nav}{body}</body></html>")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(doc.encode())

        def _redirect(self, to: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", to)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _login_form(self, err: str = "") -> None:
            self._page("Recursa · sign in", (f'<p class="err">{html.escape(err)}</p>' if err else "") +
                       '<form method="post" action="/login"><label>Reviewer id<input name="reviewer"></label>'
                       '<label>Passphrase<input name="passphrase" type="password"></label>'
                       '<button>Sign in</button></form>')

        # -- routes -------------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/login" or not self._who():
                return self._login_form()
            if self.path == "/":
                return self._home()
            if self.path.startswith("/c/"):
                return self._candidate(self.path[3:])
            self._page("Not found", "", 404)

        def do_POST(self) -> None:  # noqa: N802
            form = self._form()
            if self.path == "/login":
                try:
                    tok = auth.issue_token(form.get("reviewer", ""), form.get("passphrase", ""), channel="ui")
                except AuthError as e:
                    return self._login_form(str(e))
                return self._redirect("/", {"Set-Cookie": f"recursa={tok}; HttpOnly; SameSite=Strict; Path=/"})
            if self.path == "/logout":
                return self._redirect("/login", {"Set-Cookie": "recursa=; Max-Age=0; Path=/"})
            if not self._who() or not hmac.compare_digest(form.get("csrf", ""), self._csrf()):
                return self._page("Denied", "<p class=err>Session expired or invalid form token.</p>", 403)
            try:
                if self.path.startswith("/c/") and self.path.endswith("/decide"):
                    cid = self.path[3:-len("/decide")]
                    out = learner.decide(cid, form["decision"], self._token(), form["packet_hash"],
                                         reason=form.get("reason", ""), edited_prompt=form.get("edited") or None)
                elif self.path == "/rollback":
                    out = learner.rollback(form["version"], self._token(), form.get("reason", ""))
                else:
                    return self._page("Not found", "", 404)
            except (AuthError, RuntimeError, ValueError, KeyError) as e:
                return self._page("Error", f"<p class=err>{html.escape(str(e))}</p><a href='/'>Back</a>", 400)
            self._page("Done", f"<pre>{html.escape(json.dumps(out, indent=2))}</pre><a href='/'>Back</a>")

        def _home(self) -> None:
            q = learner.review_queue()
            rows = "".join(
                f"<tr><td><a href='/c/{c.id}'>{c.id}</a></td><td>{c.baseline_version}</td>"
                f"<td>{html.escape(c.rationale[:120])}</td></tr>" for c in q) or \
                "<tr><td colspan=3 class=muted>Nothing awaiting review.</td></tr>"
            current = learner.store.current_version()
            vrows = "".join(
                f"<tr><td>{'● ' if v['id'] == current else ''}{v['id']}</td><td>{v['parent'] or ''}</td>"
                f"<td>{html.escape(', '.join(v['approvers']))}</td><td>{v['candidate_id'] or ''}</td></tr>"
                for v in learner.store.list_versions())
            arows = "".join(
                f"<tr><td>{r['seq']}</td><td>{html.escape(r['actor'])}</td><td>{html.escape(r['action'])}</td>"
                f"<td>{html.escape(r['subject'])}</td></tr>" for r in learner.store.audit_log()[-15:][::-1])
            body = (f"<h2>Awaiting review</h2><table><tr><th>Candidate</th><th>Baseline</th><th>Why</th></tr>{rows}"
                    f"</table><h2>Versions</h2><table><tr><th>Version</th><th>Parent</th><th>Approved by</th>"
                    f"<th>Candidate</th></tr>{vrows}</table>"
                    f"<form method=post action=/rollback><input type=hidden name=csrf value='{self._csrf()}'>"
                    f"<label>Roll back to version<input name=version placeholder='v1'></label>"
                    f"<label>Reason<input name=reason></label><button>Roll back</button></form>"
                    f"<h2>Recent audit</h2><table>{arows}</table>")
            self._page("Recursa · " + learner.cfg.project, body)

        def _candidate(self, cid: str) -> None:
            try:
                h, p = learner.packet(cid)
            except KeyError:
                return self._page("Not found", "<p>No review packet.</p>", 404)
            c = learner.store.get_candidate(cid)
            form = ""
            if c and c.status == "awaiting_review":
                form = (f"<form method=post action='/c/{cid}/decide'><input type=hidden name=csrf value='{self._csrf()}'>"
                        f"<input type=hidden name=packet_hash value='{h}'>"
                        "<label>Reason (required to reject or request changes)<textarea name=reason rows=3>"
                        "</textarea></label><label>Edited prompt (optional, with request changes)"
                        "<textarea name=edited rows=6></textarea></label>"
                        "<button name=decision value=approve>Approve and promote</button>"
                        "<button name=decision value=request_changes>Request changes</button>"
                        "<button name=decision value=reject>Reject</button></form>")
            self._page(f"Candidate {cid}", f"<p class=muted>Status: {c.status if c else '?'}</p>"
                       f"<pre>{html.escape(render_packet(h, p))}</pre>{form}")

    srv = HTTPServer((host, port), H)
    print(f"Recursa review UI on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
