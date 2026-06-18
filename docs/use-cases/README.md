# Use-case runsheets

Step-by-step recipes for end-to-end SHIPS workflows.

Each runsheet here describes a real operational scenario from raw input
to deliverable, listing the exact subcommands and the flags that matter
for that case. They complement (rather than replace):

- [USER_GUIDE.md](../USER_GUIDE.md) — narrative walkthrough of the framework.
- [SHIPS_MODULE_ARGS.md](../SHIPS_MODULE_ARGS.md) — exhaustive flag reference for every subcommand.
- [RUNSHEET_EXAMPLES.md](../RUNSHEET_EXAMPLES.md) — short, single-command snippets.

## Available runsheets

- [tokenised-payload-multi-env-package.md](tokenised-payload-multi-env-package.md) — harvest legacy DDL into a tokenised payload, then build environment-specific packages from it. The primary SHIPS use case.

## Adding a new runsheet

1. Pick a short kebab-case filename that names the scenario, not the
   command (`legacy-import-onboarding.md`, not `import-legacy-cli.md`).
2. Open with a one-paragraph statement of the user goal — what success
   looks like, not how SHIPS gets there.
3. Number the steps in operational order; for each step give the
   command, then a table of flags with one-line "why this flag for
   this scenario" notes.
4. Close with the one-shot equivalent (where one exists) and any
   common variations.
5. Link the new file from the list above.
