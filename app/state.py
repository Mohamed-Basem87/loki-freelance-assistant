import json
import os
from pathlib import Path


STATE_FILE = Path(__file__).resolve().parent.parent / "database" / "state.json"

# Top-level key holding FreeHub's per-source "seen project uid" lists.
# Kept separate from the per-channel Telegram watermarks (which live
# as bare top-level keys, e.g. "-1001234": 5678) so the two schemas
# don't collide.
_FREEHUB_KEY = "_freehub_seen"


class StateManager:
    def __init__(self):
        self.data = {}

    def load(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if STATE_FILE.exists():
            try:
                self.data = json.loads(STATE_FILE.read_text())
            except Exception:
                self.data = {}
        else:
            self.data = {}
            self.save()

    def save(self):
        # Write atomically: a crash or power-loss mid-write to the
        # *actual* state file would corrupt it, and load() treats a
        # corrupt file as "no state at all" -- silently resetting
        # every channel/source back to never-seen and triggering a
        # full re-recovery (duplicate notifications) on next startup.
        # Writing to a temp file and rename()-ing over the real path
        # (same pattern already used by app.logger.ExcelLogger.save)
        # means the real file is only ever replaced by a complete,
        # valid write.
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        temp_path = STATE_FILE.with_suffix(".tmp.json")

        temp_path.write_text(
            json.dumps(
                self.data,
                indent=4,
            )
        )

        os.replace(temp_path, STATE_FILE)

    def get_last_message_id(self, channel_id):
        return int(self.data.get(str(channel_id), 0))

    def set_last_message_id(self, channel_id, message_id):
        self.data[str(channel_id)] = message_id
        self.save()

    # ------------------------------------------------------------
    # FreeHub dedup persistence.
    #
    # Previously this lived entirely in an in-memory deque in
    # app.freehub, which meant every process restart silently reset
    # it -- any project posted between shutdown and the next
    # successful poll was never recovered (worse than the Telegram
    # side, which at least persisted a watermark). Persisting the
    # same "seen" list here, alongside the Telegram state, gives
    # FreeHub the same restart-survives-recovery guarantee.
    # ------------------------------------------------------------

    def get_freehub_seen(self, source: str) -> list:
        return list(self.data.get(_FREEHUB_KEY, {}).get(source, []))

    def set_freehub_seen(self, source: str, seen_ids: list):
        bucket = self.data.setdefault(_FREEHUB_KEY, {})
        bucket[source] = list(seen_ids)
        self.save()


state = StateManager()
