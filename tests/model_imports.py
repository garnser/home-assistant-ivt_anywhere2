"""Load pure model functions without importing the Home Assistant integration."""

import importlib
import sys
from pathlib import Path
from types import ModuleType

PACKAGE = "ivt_model_test"
package = ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components/ivt_anywhere2")
]
sys.modules[PACKAGE] = package
for name in ("model_config", "estimate_math", "learning", "month", "gap_fill"):
    importlib.import_module(f"{PACKAGE}.{name}")
