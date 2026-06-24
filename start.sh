#!/bin/bash
cd /root/storerip
source .env 2>/dev/null || true
pip3 install -r requirements.txt --break-system-packages -q
python3 app.py
