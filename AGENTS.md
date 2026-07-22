# ManabiGrid site update router

- Read `UPDATE_CONTRACT.md` before diagnosing, building, or publishing this site.
- Treat `https://github.com/ManabiGrid/manabigrid` as read-only canonical content.
- For a routine source-only update, do not edit generated HTML or relax checks; use `python3 update_pages.py status` and the contract's single publish command.
- `--approve-publication` is a technical gate, not authority. Supply it only when the active user request explicitly authorizes this Pages publication.
- Stop on a named blocked/failed status. Preserve the last successful Pages deployment and report the exact gate; never guess a fix or a source SHA.
- Do not add dependencies, change remotes or account settings, delete data, or change deployment policy without separate approval.
