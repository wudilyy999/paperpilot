"""
Build sample data for the static PaperPilot dashboard.

Example:
    python examples/storm_examples/build_paperpilot_demo_bundle.py \
        --output-dir frontend/paperpilot_dashboard
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from knowledge_storm.paperpilot_demo import build_demo_bundle


def main():
    parser = argparse.ArgumentParser(description="Build PaperPilot dashboard sample data.")
    parser.add_argument("--output-dir", default="frontend/paperpilot_dashboard")
    args = parser.parse_args()
    bundle = build_demo_bundle(Path(args.output_dir))
    print(json.dumps(bundle, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
