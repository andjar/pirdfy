#!/usr/bin/env python3
"""
Development runner for Pirdfy.
Run this script to start Pirdfy in development mode without full installation.

Usage:
    python run_dev.py [--port PORT] [--debug]
"""

import sys
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

if __name__ == "__main__":
    from main import main
    main()
