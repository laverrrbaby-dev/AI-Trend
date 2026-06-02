#!/bin/bash
cd "$(dirname "$0")"
exec /usr/bin/env python3 scripts/launch_site.py
