# ADR 0010: UTF-8 Session Charset for All teradatasql Connections

## Status

Accepted | 2026-05-06

## Context

SHIPS deploys DML files that contain rich documentation strings — design
decision rationale, business glossary definitions, query cookbook entries,
and implementation notes. These strings routinely use typographic
punctuation such as the em dash (— U+2014), which falls outside the
ASCII range.

The Python `teradatasql` driver negotiates a session character set with
the Teradata server at connection time. When no charset is specified the
driver defaults to the session character set configured on the server,
which is typically **Latin** (ISO 8859-1) on enterprise Teradata
installations. Latin cannot represent characters above U+00FF.

The symptom is Teradata **Error 6706: The string contains an untranslatable
character**, raised at execution time when any statement contains a
character outside the session charset. The error is non-obvious because:

1. The same SQL executes without error from SQL Assistant via the .NET
   ODBC/JDBC connector, which negotiates UTF-16 automatically.
2. The failure occurs at runtime, after the connection pool is open and
   waves are executing — not at preflight, where a developer would
   expect configuration problems to surface.
3. The error message names no character or column, requiring a binary
   search through the DML file to identify the offending codepoint.

The affected connection sites are:

- `database_package_deployer/cli.py` — the `_connect()` function used by the
  deployer CLI and the connection pool factory.
- The `connect()` function in the generated `deploy.py` script embedded
  in every release package.

## Decision

Every `teradatasql.connect()` call in SHIPS passes **`charset="UTF8"`**
explicitly, regardless of the server's default session character set.

```python
params = {"host": host, "user": user, "charset": "UTF8"}
```

UTF-8 was chosen over UTF-16 because:

- UTF-8 is the standard charset for Python source files, log files, and
  JSON payloads. Keeping the session charset aligned with the file
  encoding avoids silent re-encoding at string boundaries.
- UTF-8 covers all Unicode codepoints that will appear in SHIPS
  documentation strings (BMP range is sufficient; no surrogate pairs
  expected).
- UTF-16 would require two-byte encoding of all ASCII characters,
  roughly doubling wire payload for SQL-heavy sessions. An irrelevant
  cost for a deployment tool, but needlessly wasteful.

## Consequences

**Positive**

- Error 6706 is eliminated for all Unicode characters in SQL string
  literals, regardless of server default charset.
- Behaviour is now consistent with .NET and JDBC connectors used by
  DBAs testing the same SQL in SQL Assistant.
- Rich Unicode content in documentation DML (em dashes, typographic
  quotes, non-ASCII business terminology) is supported without sanitising
  author intent.
- The charset is explicit and visible in the code rather than being an
  invisible server-side default.

**Negative**

- If a target Teradata server has UTF-8 disabled (uncommon but possible
  on very old installations), connections will fail at the charset
  negotiation step rather than at statement execution. The failure mode
  is earlier and clearer, but it is a new failure mode.
- All `teradatasql.connect()` call sites must carry the `charset`
  parameter. A future connection helper that omits it will silently
  reintroduce the problem. The fix must be enforced by code review
  convention, not by automated constraint.

**Neutral**

- Existing deployments of SQL containing only ASCII are unaffected:
  UTF-8 is a superset of ASCII; all ASCII strings are valid UTF-8.
- The `.sha256` archive checksum and `package_integrity.json` fingerprint
  (ADR 0011) are computed over raw file bytes and are charset-agnostic.

## Alternatives considered

**Replace Unicode characters with ASCII equivalents in DML source.**
Rejected: em dashes and typographic punctuation in documentation strings
carry authorial meaning. Mandating ASCII-only source would reduce
documentation quality and create an ongoing friction point with no
technical benefit given that UTF-8 is available.

**UTF-16 charset.** Rejected on efficiency grounds. UTF-16 doubles the
wire size of ASCII-dominant SQL text. No benefit over UTF-8 for the
codepoints SHIPS uses.

**Sanitise strings at harvest time (strip or replace non-ASCII).**
Rejected: the deployer should execute what the developer wrote. Silent
mutation of SQL content at build time is worse than the original Error
6706 — at least 6706 is visible.

**Document the error and require developers to avoid Unicode in DML.**
Rejected: the restriction is not discoverable, imposes ongoing friction,
and is invisible to contributors using SQL Assistant (where the same
strings work). The fix is two words; the workaround is a permanent
cognitive tax.

## References

- `database_package_deployer/cli.py` — `_connect()` function (the fix site for the
  deployer CLI).
- `td_release_packager/builder.py` — `connect()` function inside
  `_generate_deploy_script()` (the fix site for the generated package
  entry point).
- Teradata error 6706 documentation: session charset negotiation occurs
  at logon; character translation failure is raised at statement
  execution, not at parse time.
- ADR 0011: SHA-256 package integrity fingerprinting — implemented in
  the same session as this fix; both changes appear in the same commit.
