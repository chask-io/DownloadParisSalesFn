"""Mock external dependencies before any test imports."""

import sys
from unittest.mock import MagicMock
from pathlib import Path

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Mock all external modules before function_logic gets imported
_MOCKED_MODULES = [
    "selenium",
    "selenium.webdriver",
    "selenium.webdriver.remote",
    "selenium.webdriver.remote.remote_connection",
    "browserbase",
    "requests",
    "chask_foundation",
    "chask_foundation.backend",
    "chask_foundation.backend.models",
    "chask_foundation.configs",
    "chask_foundation.configs.utils",
    "api",
    "api.files_requests",
    "api.widget_resolver",
]

for mod in _MOCKED_MODULES:
    sys.modules[mod] = MagicMock()

# Make webdriver.Remote usable as a type hint
sys.modules["selenium.webdriver"].Remote = MagicMock
sys.modules["selenium.webdriver"].ChromeOptions = MagicMock
sys.modules["selenium.webdriver.remote.remote_connection"].RemoteConnection = type(
    "RemoteConnection", (), {"__init__": lambda *a, **kw: None, "get_remote_connection_headers": lambda *a, **kw: {}}
)
