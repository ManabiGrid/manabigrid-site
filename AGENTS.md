# ManabiGrid site update router

- Read `UPDATE_CONTRACT.md` before diagnosing, building, or publishing this site.
- Treat `https://github.com/ManabiGrid/manabigrid` as read-only canonical content.
- For a routine source-only update, do not edit generated HTML or relax checks; use `python3 update_pages.py status` and the contract's single publish command.
- `python3 update_pages.py status` is stdout-only. It must not create or update any file, dispatch a workflow, or change GitHub state.
- Treat ignored `update-report.json` only as the last fully verified publication record. Never overwrite it with status, dry-run, blocked-before-publication, or generic error output.
- Site-code changes go through `.github/workflows/pr-validate.yml`: all tests, fresh build/check/quarantine, the 11-profile real-browser matrix, CSS-overflow rejection, and the final canonical-source SHA recheck must pass against `site-output`. Do not add a bypass condition or deploy permission.
- Changes to gate-core files (workflow, checkers, public allowlist, browser verifier, tests, or MathML registry) need independent review. Never weaken a validator and its expectation in the same change merely to make the gate green.
- Inline MathML may be added only by extending the fixed registry and its exact tree/count/negative contracts. Similar-looking text is not approval.
- `--approve-publication` is a technical gate, not authority. Supply it only when the active user request explicitly authorizes this Pages publication.
- Stop on a named blocked/failed status. Preserve the last successful Pages deployment and report the exact gate; never guess a fix or a source SHA.
- Do not add dependencies, change remotes or account settings, delete data, or change deployment policy without separate approval.
