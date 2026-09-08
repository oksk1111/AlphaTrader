"""루트 conftest — 테스트에서 저장소 루트를 임포트 경로에 넣는다.

`python -m pytest` 는 현재 디렉터리를 sys.path 에 자동으로 넣어주지만,
`pytest` 를 직접 실행하면 그렇지 않다. 그래서 로컬에서는 통과하던 테스트가
CI 에서 `ModuleNotFoundError: No module named 'modules'` 로 깨졌다.
실행 방식에 상관없이 동작하도록 여기서 한 번만 보정한다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
