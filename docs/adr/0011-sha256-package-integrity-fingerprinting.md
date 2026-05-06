# ADR 0011: SHA-256 Package Integrity Fingerprinting

## Status

Accepted | 2026-05-06

## Context

A SHIPS release package is built by a developer or CI pipeline, archived
as a `.zip`, and then handed to a DBA for deployment — potentially across
an air gap, an SFTP transfer, a shared drive, or a manual extraction
step. Each of these handoff stages is an opportunity for the package
contents to be modified, either accidentally (wrong file copied, partial
extraction) or deliberately (payload substitution).

The existing `.sha256` checksum sidecar (generated alongside the `.zip`)
addresses **archive-level integrity**: it confirms the zip file was not
corrupted in transit. It provides no guarantee once the archive is
extracted, because:

- The `.sha256` covers the zip container, not the individual files inside
  it. After extraction the zip is no longer in the picture.
- A DBA who receives the extracted directory — which is the typical
  handoff for environments where Python-unzip is not available — has no
  checksum to verify at all.
- The `.sha256` file is a sibling of the zip, not embedded in the
  package. It can be misplaced or omitted without affecting the
  `deploy.py` execution path.

The deeper concern is **chain of custody**: when `deploy.py` opens a
database connection and begins executing DDL, there is no recorded
evidence that the payload it is executing is byte-for-byte identical to
what the packager produced. In regulated environments (SOX, APRA
operational risk controls) this gap is an audit finding.

A secondary value is the **audit trail**: Teradata logs every SQL
execution in DBQL against the session's query band. If the package
fingerprint is carried in the query band, every DBQL record for a
deployment is permanently associated with the exact package version that
produced it.

## Decision

The packager computes a **SHA-256 fingerprint over every file under
`payload/`** at the end of the build, before archiving. The fingerprint
is stored in `package_integrity.json` in the package root alongside
`BUILD.json` and `deploy.py`.

**Fingerprint derivation:**

1. Walk `payload/` recursively (depth-first, directories and filenames
   sorted for determinism).
2. SHA-256 each file's raw bytes.
3. Concatenate the sorted entries as `"relative/path:filehash\n"`.
4. SHA-256 the concatenation to produce the single `package_hash`.

```json
{
  "algorithm": "SHA-256",
  "package_hash": "a3f8c2d1e9b74056...",
  "file_count": 119,
  "files": {
    "payload/03_ddl/MortgagePlatform_Domain.Customer_H.ddl": "9e4c1a...",
    "payload/04_dml/02_memory_documentation.multi_table.dml": "b72f09...",
    ...
  }
}
```

The generated `deploy.py` (embedded in every package) calls
`_verify_integrity()` **after logging setup and before any database
connection is opened**. The verifier:

1. Recomputes all file hashes.
2. Checks for added, removed, or modified files against `package_integrity.json`.
3. Recomputes `package_hash` and compares with the stored value.
4. Aborts with a non-zero exit code and a diagnostic log if any
   discrepancy is found.

On successful verification the `package_hash` is carried into every
Teradata session query band as `PKG_HASH=<first 16 hex chars>`:

```
BUILD=0002;PKG=MortgagePlatform_SHIPS;ENV=DEV;PKG_HASH=a3f8c2d1e9b74056;DEPLOYER=ddl_deployer_v2;
```

This makes the fingerprint a permanent part of the DBQL audit record for
every SQL statement executed during deployment.

A `--skip-integrity-check` flag provides a development escape hatch.
Its use is logged at WARNING level. When skipped, `PKG_HASH=SKIPPED`
appears in the query band so the bypass is visible in DBQL.

For auto-split packages (prereqs + main), `package_integrity.json` is
generated independently for each half after the phase partitioning,
so each archive's fingerprint covers only its own payload.

## Consequences

**Positive**

- Any modification to any payload file — intentional or accidental —
  changes the fingerprint and blocks deployment before the database is
  contacted. No SQL is executed against a tampered package.
- The fingerprint covers the files that matter (payload SQL), not the
  container. Extraction, renaming, or repackaging does not defeat the
  check.
- `PKG_HASH` in DBQL provides a permanent, query-able link between every
  executed statement and the exact package version. Audit queries can
  confirm "all DDL in this deployment came from build 0002 with hash
  a3f8c2d1e9b74056."
- Individual file hashes in `package_integrity.json` allow precise
  forensic identification of which file was changed, not just that
  *something* changed.
- The `--skip-integrity-check` bypass is recorded in both the deploy log
  and the query band, so it cannot be used silently.

**Negative**

- The trust guarantee is **chain-of-custody**, not **adversarial**. An
  attacker with write access to the extracted package directory can
  modify both a payload file and `package_integrity.json` to match,
  defeating the check. The hash provides no protection against an
  attacker who controls the package directory. Protecting against that
  threat requires asymmetric signing (see Alternatives).
- `package_integrity.json` is generated before archiving and lives
  inside the archive. It is regenerated correctly by the build pipeline
  on every build. However, a developer who manually edits a payload file
  after extraction and then also manually recomputes `package_integrity.json`
  could produce a consistent but fraudulent pair. This is a human
  control problem, not a technical one; the fix is key-based signing.
- The `PKG_HASH` field in the query band is 16 hex characters (64 bits
  of the 256-bit hash). It is not collision-resistant at that truncation
  but is sufficient for DBQL correlation. The full hash is in
  `package_integrity.json` and in the deploy log for forensic use.

**Neutral**

- The existing archive-level `.sha256` sidecar is retained. It and
  `package_integrity.json` serve complementary purposes: `.sha256`
  covers transit integrity of the zip container; `package_integrity.json`
  covers deployment integrity of the extracted payload.
- File hashing adds negligible time to the build for typical package
  sizes (< 1 second for hundreds of files).
- The `--skip-integrity-check` flag will appear in `--help` output.
  Teams should treat its presence in deployment logs as a review trigger,
  not a routine occurrence.

## Alternatives considered

**Archive-level checksum only (status quo).** Rejected: the `.sha256`
provides no guarantee after extraction, covers the wrong boundary, and
is not embedded in the package's own execution path.

**MD5 or SHA-1.** Rejected: both algorithms have known collision attacks.
SHA-256 is the current NIST standard for integrity verification and adds
no meaningful performance cost for file sizes in scope.

**Asymmetric signing (RSA / ECDSA).** Considered as the primary option.
Deferred. Signing would bind the `package_hash` to a private key held
outside the package directory, making it impossible to forge a valid
signature even with full write access to the extracted package.
This is the correct solution for adversarial threat models (insider
threat, SOX-grade controls). It requires key management infrastructure
(key generation, distribution to deployment hosts, rotation, revocation)
that is not yet in place. The SHA-256 hash approach is implemented now
as the foundation; the signing layer will be added when the key
management infrastructure exists.
See [GitHub issue #76](https://github.com/earthshiner/teradata-deployment-agent/issues/76).

**Merkle tree (file-level hash chain).** Considered. Would allow proving
that a single file is part of a valid package without revealing all
other hashes. The use case (selective verification of one file) is not
a SHIPS deployment pattern. A flat sorted-concatenation hash is simpler
and achieves the same tamper detection for the full-package case.

**Embed fingerprint in `BUILD.json` rather than a separate file.**
Rejected: `BUILD.json` is a manifest written during the build and is
used by downstream tooling (deploy report, split-package detection).
Embedding a computed field in it would require computing the fingerprint
before the manifest is finalised, or a two-pass write. A separate
`package_integrity.json` keeps the concerns separate and matches the
single-responsibility principle established for other generated files.

## References

- `td_release_packager/builder.py` — `_generate_integrity_file()` (the
  fingerprint generator, called before `_archive_package()` in both the
  single-package and auto-split paths).
- `td_release_packager/builder.py` — `_generate_deploy_script()` (the
  template that embeds `_verify_integrity()` and the `--skip-integrity-check`
  argument into every generated `deploy.py`).
- ADR 0010: UTF-8 session charset — implemented in the same session;
  both changes appear in the same commit.
- GitHub issue #76: asymmetric signing layer (the deferred adversarial
  trust guarantee).
- NIST FIPS 180-4: Secure Hash Standard (SHA-256 specification).
