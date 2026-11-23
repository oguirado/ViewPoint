"""Exit after an inert send and before C1 can complete the durable claim."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from json_fixtures import Harness, read_json


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    Harness(directory, exit_after_send=True).call(sys.argv[2], read_json(directory / "crash-input.json"))
    raise SystemExit("fixture send did not reach its crash point")
