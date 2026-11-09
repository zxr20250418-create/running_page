import json
import os

from config import SYNCED_FILE
from gpx_dedup import atomic_json


def save_synced_data_file_list(file_list: list):
    old_list = load_synced_file_list()

    # Preserve source filenames, including excluded duplicates; do not mutate
    # the caller's list or accumulate repeated entries on retries.
    atomic_json(SYNCED_FILE, list(dict.fromkeys([*file_list, *old_list])))


def load_synced_file_list():
    if os.path.exists(SYNCED_FILE):
        with open(SYNCED_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception as e:  # noqa: BLE001
                print(f"json load {SYNCED_FILE} \nerror {e}")

    return []
