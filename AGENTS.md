
## BeamNG.drive compatibility requirements

- Target game version: BeamNG.drive 0.40.1.0.
- All Lua code, test helpers, diagnostics and launch instructions MUST be compatible with BeamNG.drive 0.40.1.
- NEVER use getUserPath(). It is unavailable in this version.
- Use FS:getFileRealPath("/") to resolve the current user profile directory.
- Never assume an API exists because it works in a newer BeamNG version.
- Verify compatibility before packaging any manual engine-test kit.
- Preserve the user's isolated profiles, test evidence and historical worktrees.
