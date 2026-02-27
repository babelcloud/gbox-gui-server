# gbox-gui-server

A local REST API server that implements the [gbox.ai](https://docs.gbox.ai) UI Action, Command, and File System APIs using `pyautogui`.

Listens on `0.0.0.0:5789`. No authentication required.

## Requirements

- Python 3.11+
- Windows (x64 or arm64) — primary target platform
- macOS / Linux supported for development

## Installation

```bash
pip install -r requirements.txt
```

## Running

### Direct (foreground)

```bash
python server.py
```

The server starts at `http://127.0.0.1:5789`.

### Windows — pre-built executable

Download the latest `server-windows-x64.exe` or `server-windows-arm64.exe` from the [Releases](../../releases) page.

#### Run directly (foreground / debug)

```cmd
gbox-gui-server.exe --console
```

#### Install and run as a Windows Service (recommended)

The service registers itself with the Windows Service Control Manager (SCM) and starts automatically on boot.

**Architecture:**

```
Boot
└─ Session 0  ←  SCM starts GBOXGUIServer service (parent process, no desktop)
                  │
                  │  WTSQueryUserToken + CreateProcessAsUser
                  ▼
   User logs in → Session 1 ← hidden child process runs Flask + pyautogui
                               (full desktop access, no console window)
```

The parent service monitors the child process and **automatically restarts it within 10 seconds** if it crashes.

**Service management** (run as Administrator):

```cmd
# Install the service (auto-start on boot)
gbox-gui-server.exe install

# Start immediately (without rebooting)
gbox-gui-server.exe start

# Stop the service
gbox-gui-server.exe stop

# Uninstall the service
gbox-gui-server.exe remove
```

After `install`, the service is visible in `services.msc` as **GBOX GUI Server** and will start automatically on every boot.

## Building from source

Requires Windows and Python 3.11.

```cmd
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --onefile --name gbox-gui-server ^
  --hidden-import win32timezone ^
  --hidden-import win32serviceutil ^
  --hidden-import win32service ^
  --hidden-import win32event ^
  --hidden-import win32ts ^
  --hidden-import win32process ^
  --hidden-import win32profile ^
  --hidden-import win32security ^
  --hidden-import win32con ^
  --hidden-import servicemanager ^
  --collect-binaries pywin32 ^
  server.py
```

The executable is produced at `dist/gbox-gui-server.exe`.

CI/CD builds for both x64 and arm64 are triggered automatically on every push via GitHub Actions.

## API Endpoints

All paths mirror the gbox.ai API with the `boxId` path segment removed.

### Health check

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Returns `{"status":"ok","platform":"Windows"}` |

### UI Actions

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/actions/screenshot` | Take a screenshot (returns base64 PNG) |
| POST | `/api/v1/actions/click` | Click at coordinates |
| POST | `/api/v1/actions/move` | Move mouse to position |
| POST | `/api/v1/actions/type` | Type text |
| POST | `/api/v1/actions/press-key` | Press keyboard keys / shortcuts |
| POST | `/api/v1/actions/scroll` | Scroll (direction or coordinates) |
| POST | `/api/v1/actions/drag` | Drag from start to end |
| GET  | `/api/v1/actions/clipboard` | Get clipboard content |
| POST | `/api/v1/actions/clipboard` | Set clipboard content |

### Commands

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/commands` | Execute a shell command |

### File System

| Method | Path | Description |
|--------|------|-------------|
| GET    | `/api/v1/fs/list`   | List directory contents |
| GET    | `/api/v1/fs/read`   | Read file content |
| POST   | `/api/v1/fs/write`  | Write/create a file |
| DELETE | `/api/v1/fs`        | Delete a file or directory |
| POST   | `/api/v1/fs/exists` | Check if a path exists |
| POST   | `/api/v1/fs/rename` | Rename a file or directory |
| GET    | `/api/v1/fs/info`   | Get file/dir metadata |

## Examples

```bash
# Health check
curl http://127.0.0.1:5789/

# Take a screenshot
curl -X POST http://127.0.0.1:5789/api/v1/actions/screenshot \
  -H "Content-Type: application/json" -d "{}"

# Click at (100, 200)
curl -X POST http://127.0.0.1:5789/api/v1/actions/click \
  -H "Content-Type: application/json" -d "{\"x\":100,\"y\":200}"

# Type text
curl -X POST http://127.0.0.1:5789/api/v1/actions/type \
  -H "Content-Type: application/json" -d "{\"text\":\"Hello World\"}"

# Press Ctrl+C
curl -X POST http://127.0.0.1:5789/api/v1/actions/press-key \
  -H "Content-Type: application/json" -d "{\"keys\":[\"control\",\"c\"]}"

# Run a command
curl -X POST http://127.0.0.1:5789/api/v1/commands \
  -H "Content-Type: application/json" -d "{\"command\":\"echo hello\"}"

# List directory
curl "http://127.0.0.1:5789/api/v1/fs/list?path=C:/Users"
```

## Notes

- Natural language targets (e.g., `"target": "login button"`) are **not supported** — use coordinates instead.
- Screenshot `outputFormat: storageKey` is not supported — responses always use `base64`.
- The Windows service runs as **LocalSystem**. `WTSQueryUserToken` requires this privilege to inject the child process into the user's desktop session.
- On lock/logoff the child process is stopped; it is restarted automatically on unlock/logon.
