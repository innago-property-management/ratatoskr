#!/bin/sh
python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-3000}/health')"
