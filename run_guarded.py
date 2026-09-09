"""Guaranteed-guarded entry point.

Regression fix (audit finding P2-3): previously this file was
byte-for-byte identical to run.py -- both simply called
`app.bot.main()`, and whether the Notification Guard actually ran was
decided entirely by the NOTIFICATION_GUARD_ENABLED environment
variable at deploy time. An operator could run
`python run_guarded.py` with that variable unset/false and reasonably
but incorrectly assume the guard was active, since nothing about this
file's own behavior reflected its name.

This entry point now explicitly forces the guard on (Option A from
the audit's own two suggested resolutions -- keeping this as a
distinct, meaningfully-different entrypoint rather than deleting it,
since existing deployments/Dockerfiles may already invoke it by name)
by setting NOTIFICATION_GUARD_ENABLED=true in the process environment
before importing anything that reads it. app.notification_guard.config
reads this variable once at import time, so the override MUST happen
before app.bot (or anything it imports) is imported -- ordering here
is load-bearing, not stylistic.

If the guard's own required configuration (a Groq API key, a model
list) is missing, app.notification_guard.config raises at import time
exactly as it would for NOTIFICATION_GUARD_ENABLED=true set any other
way -- this entrypoint intentionally does not swallow that error,
since silently falling back to unguarded delivery would defeat the
entire point of choosing this entrypoint by name.
"""
import os

os.environ["NOTIFICATION_GUARD_ENABLED"] = "true"

from app.bot import main

if __name__ == "__main__":
    main()
