"""Every file location in one place, so no module hard-codes paths."""
import os
from pathlib import Path

# Where the project lives. `pip install .` puts the package in site-packages,
# where data must not be written and .github/ does not exist at all - so an
# installed copy needs to be told where the real checkout is.
ROOT = Path(os.environ.get("POSTCADENCE_HOME") or Path(__file__).resolve().parent.parent)
DATA_DIR = ROOT / "data"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
TOPICS_FILE = DATA_DIR / "topics.json"
DRAFTS_FILE = DATA_DIR / "drafts.json"
