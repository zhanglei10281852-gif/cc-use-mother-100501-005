"""地下管网施工冲突协同命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from utility_coordination import WorkPermit


def main() -> None:
    item = WorkPermit(permit_code='permit-code-001', corridor_code='corridor-code-001', revision=1, state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
