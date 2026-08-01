#!/bin/sh
cd "$(dirname "$0")" || exit 1
printf '%s\n' 'Weboldal: http://localhost:8000/'
printf '%s\n' 'Admin:    http://localhost:8000/admin'
exec python3 server.py
