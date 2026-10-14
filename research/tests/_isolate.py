"""The product tests' isolation (an empty temporary Graph-MIND home), with research/ importable."""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "research"))
_spec = importlib.util.spec_from_file_location("_product_isolate", _ROOT / "tests" / "_isolate.py")
_spec.loader.exec_module(importlib.util.module_from_spec(_spec))
