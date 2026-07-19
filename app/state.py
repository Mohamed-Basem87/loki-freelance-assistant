import json
from pathlib import Path


STATE_FILE = Path(__file__).resolve().parent.parent / "database" / "state.json"


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
        STATE_FILE.write_text(
            json.dumps(
                self.data,
                indent=4,
            )
        )

    def get_last_message_id(self, channel_id):
        return int(self.data.get(str(channel_id), 0))

    def set_last_message_id(self, channel_id, message_id):
        self.data[str(channel_id)] = message_id
        self.save()


state = StateManager()
