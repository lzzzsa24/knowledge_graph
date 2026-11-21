"""Preview or publish the Adeia source review."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kg_pilot.quality_review_publish import main


if __name__ == "__main__":
    main()
