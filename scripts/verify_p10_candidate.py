from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = REPO_ROOT.parent
sys.path.insert(0, str(PACKAGE_PARENT))

from astrbot_plugin_shio.scripts.build_p10_candidate import verify_candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deterministic Shio P10 release candidate.")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    result = verify_candidate(args.archive, args.manifest)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
