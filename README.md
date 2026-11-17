# Runtime Service

Runtime state is saved every 180 seconds and again during normal shutdown or
cancellation. SQLite databases and sessions use the online backup API, including
committed WAL pages. Snapshots are validated before atomically replacing the
backup file; a failed snapshot keeps the last good remote version.

State commits are made from a separate checkout under `RUNNER_TEMP`, guarded by
a process lock. They include only the configured state files and are rebuilt on
the latest remote head when concurrent updates reject a push. The bot checkout
is never reset, rebased, or used to commit snapshots.

Run `python -m unittest discover -s tests -v` to test concurrent SQLite writes,
WAL recovery, corrupt files, save locking, and concurrent Git pushes. The same
checks run in CI and before the runtime starts.

This remains periodic persistence: a sudden runner loss can lose changes since
the last successful push. It is not a guarantee of zero data loss. Cancellation
cleanup is best effort; backup failures are surfaced in Actions annotations.
