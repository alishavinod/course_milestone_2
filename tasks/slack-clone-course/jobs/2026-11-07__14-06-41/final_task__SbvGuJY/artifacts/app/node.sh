#!/usr/bin/env bash
exec python3 -m uvicorn app:app --host 127.0.0.1 --port "$1"
