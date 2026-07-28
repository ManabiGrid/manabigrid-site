# Claude Code entry point

Read and follow `UPDATE_CONTRACT.md` in full. It is the provider-independent contract for this repository.

For routine canonical-source updates, use only the commands documented there. Do not hand-edit generated HTML, invent a commit SHA, bypass a failing check, or treat the presence of `--approve-publication` as user authorization.

`update_pages.py status` is stdout-only. `update-report.json` is reserved for a
publication whose Actions run, public source/site SHAs, deployment, and HTTP
contract were fully verified. For site-code changes, keep the canonical source
read-only and use the PR gate documented in `UPDATE_CONTRACT.md`; do not weaken
the 11-device render matrix or extend MathML beyond its reviewed fixed registry.
If a change touches the workflow, validators, public allowlist, browser verifier,
tests, or MathML registry together, require independent review; do not update a
checker and its expected result merely to make the same change pass.
