"""Fixed input."""
#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PARENT_PATH = Path(__file__).resolve().parent / 'partition.py'


def import_parent():
    spec = importlib.util.spec_from_file_location("fixed_model_parent", PARENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(PARENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fixed_model_parent"] = module
    spec.loader.exec_module(module)
    return module
