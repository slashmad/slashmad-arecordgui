# Agent instructions

## Git commit attribution

When creating Git commits:

- Keep the repository owner's Git identity as author and committer.
- If Codex contributed to the changes, append this trailer after a blank line:

  ```text
  Co-authored-by: Codex <267193182+codex@users.noreply.github.com>
  ```

- Add the trailer only once, and only when Codex actually contributed.
- Do not attribute commits to agents that did not contribute.
- Preserve existing legitimate co-author trailers.
- Never change Git's global `user.name` or `user.email`.
