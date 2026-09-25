import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# The suite pins the deterministic compiler. Whether a key happens to be in the environment
# must never change a test outcome; the model path has its own tests, which stub the call.
#
# `make compile-live` sets LEASH_LIVE_COMPILER=1 to lift the pin for the one suite that is
# *about* the model. It is an explicit opt-in and never the default: a live run costs money,
# needs a network, and is not reproducible, so it must be something someone chose to do.
if os.environ.get("LEASH_LIVE_COMPILER") == "1":
    from leash.service.deps import load_dotenv  # noqa: E402

    load_dotenv(ROOT / ".env")
    os.environ["LEASH_COMPILER"] = "llm"
else:
    os.environ["LEASH_COMPILER"] = "baseline"
