"""Compatibility entry point for existing INA219 capture scripts and CubeIDE launches."""
import sys

import measurement


if __name__ == "__main__":
    measurement.cli()
else:
    # Existing callers can still configure the same module, including its ST-LINK settings.
    sys.modules[__name__] = measurement
