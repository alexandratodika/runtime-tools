# Python Runtime Utilities

Utilities for starting independent Python services and persisting runtime state.
Dependency locations are supplied through private runtime configuration. The
public workflow contains no source repository mapping.

SQLite state is copied through validated online backups into a separate checkout.
Saves use a process lock and preserve concurrent updates on the remote branch.
Persistence is periodic; abrupt runner loss can lose changes since the last save.

Run `python -m unittest discover -s tests -v` to validate state persistence and
dependency configuration. The workflow also runs these checks before startup.

Renaming a repository does not erase its existing history or old redirects.
