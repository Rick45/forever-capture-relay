# Repository Guidelines

## Project Structure

`ForeverClassGameplay/` is the World of Warcraft addon folder. `Core.lua` handles the tracker UI, target aura updates, and per-character frame position. `Warlock.lua` defines the tracked DoTs and curses, grouped by spell rank IDs. `ForeverClassGameplay.toc` declares client compatibility, saved variables, and Lua load order. Register new runtime Lua files in the manifest and place dependencies before the code that needs them. The repository has no separate test or asset directories.

## Build and Local Development

From the repository root, run `./build.ps1` in PowerShell. It checks that each file listed in the manifest exists and packages the addon as `dist/ForeverClassGameplay.zip`. Keep generated archives out of commits.

For local testing, copy the `ForeverClassGameplay/` folder into the matching WoW client's `Interface/AddOns/` directory. Enable the addon, log in on a Warlock, and use `/reload` after updating the installed Lua files.

## Coding Style

Use four-space indentation, double-quoted Lua strings, and descriptive lower camel case names for local variables and functions. Keep helpers local and share data through the addon namespace, as in `addon.Warlock`. Preserve the guards around restricted aura values when changing aura handling. No formatter or linter is configured.

## Testing

Run the build after changing Lua files or the manifest. There is no automated test suite. In game, apply a known DoT or curse to a hostile target, check the countdown, and verify the row updates when the effect expires or the target changes. Clear the target and confirm the tracker hides. For layout or persistence changes, Shift-drag the frame and verify its position after `/reload`.

## Commits and Pull Requests

History currently contains only `Initial commit`. Use short, descriptive subjects such as `Fix aura timer on target change`. In pull requests, summarize the behavior change, note the WoW client tested and the manual checks performed, and link relevant issues when available. Include screenshots for visible tracker changes.
