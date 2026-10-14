sudo systemctl daemon-reload
sudo systemctl enable --now battery-logger
journalctl -u battery-logger -f   # watch the live log