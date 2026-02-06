# systemd Service Templates

## agent-system.service.template

Systemd service file for running AgentSystem in production.

### Important Fix

The service includes environment variables to prevent **onnxruntime pthread_setaffinity_np crashes** (Signal 11 SEGV):

```ini
Environment="OMP_NUM_THREADS=1"
Environment="ORT_DISABLE_THREAD_AFFINITY=1"
```

These variables are **required** in containerized/virtualized environments (Docker, LXC, VMs with CPU restrictions) to prevent the ChromaDB/onnxruntime from crashing when trying to set thread affinity.

### Installation

```bash
# 1. Copy template to systemd directory
sudo cp agent-system.service.template /etc/systemd/system/agent-system.service

# 2. Edit paths and configuration as needed
sudo nano /etc/systemd/system/agent-system.service

# 3. Reload systemd daemon
sudo systemctl daemon-reload

# 4. Enable service (start on boot)
sudo systemctl enable agent-system.service

# 5. Start service
sudo systemctl start agent-system.service

# 6. Check status
sudo systemctl status agent-system.service
```

### Update Service Without Restart

If you need to update the service configuration without restarting:

```bash
# Edit the service file
sudo nano /etc/systemd/system/agent-system.service

# Reload daemon (does NOT restart the service)
sudo systemctl daemon-reload

# Changes will apply on next service restart
```

### Troubleshooting

**If you see crashes with `pthread_setaffinity_np failed`:**
- Ensure `OMP_NUM_THREADS=1` is set
- Ensure `ORT_DISABLE_THREAD_AFFINITY=1` is set
- Run `systemctl daemon-reload` after editing
- Restart service: `sudo systemctl restart agent-system.service`

**View logs:**
```bash
# Recent logs
sudo journalctl -u agent-system.service -n 100

# Follow logs in real-time
sudo journalctl -u agent-system.service -f

# Logs since a specific time
sudo journalctl -u agent-system.service --since "2026-01-07 14:00"
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_LOG_LEVEL` | `debug` | Logging level (debug, info, warning, error) |
| `OMP_NUM_THREADS` | `1` | Limit OpenMP threads (prevents affinity crashes) |
| `ORT_DISABLE_THREAD_AFFINITY` | `1` | Disable onnxruntime thread affinity (prevents SEGV) |
| `AGENT_ENABLE_PROFILING` | unset | Enable performance profiling |
| `AGENT_ENABLE_MEMORY_PROFILING` | unset | Enable memory profiling |
| `AGENT_MEMORY_SNAPSHOT_INTERVAL` | `3600` | Memory snapshot interval (seconds) |
