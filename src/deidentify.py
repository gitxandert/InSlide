#!/usr/bin/env python3
"""CLI for in-place whole-slide image deidentification."""

import sys

import deidentify_anonymize


if __name__ == "__main__":
    sys.exit(deidentify_anonymize.main())
