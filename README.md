# gbox-gui-server

A local REST API server that implements the [gbox.ai](https://docs.gbox.ai) UI Action, Command, and File System APIs using `pyautogui`.

Listens on `0.0.0.0:5789`. No authentication required.

## Installation

```bash
pip install -r requirements.txt
```

## Running

```bash
python server.py
```

The server starts at `http://127.0.0.1:5789`.

## API Endpoints

All paths mirror the gbox.ai API with the `boxId` path segment removed.

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
# Take a screenshot
curl -X POST http://127.0.0.1:5789/api/v1/actions/screenshot -H "Content-Type: application/json" -d "{}"

# Click at (100, 200)
curl -X POST http://127.0.0.1:5789/api/v1/actions/click -H "Content-Type: application/json" -d "{\"x\":100,\"y\":200}"

# Type text
curl -X POST http://127.0.0.1:5789/api/v1/actions/type -H "Content-Type: application/json" -d "{\"text\":\"Hello World\"}"

# Press Ctrl+C
curl -X POST http://127.0.0.1:5789/api/v1/actions/press-key -H "Content-Type: application/json" -d "{\"keys\":[\"control\",\"c\"]}"

# Run a command
curl -X POST http://127.0.0.1:5789/api/v1/commands -H "Content-Type: application/json" -d "{\"command\":\"echo hello\"}"

# List directory
curl "http://127.0.0.1:5789/api/v1/fs/list?path=C:/Users"
```

## Notes

- Natural language targets (e.g., `"target": "login button"`) are **not supported** â€” use coordinates instead.
- Screenshot `outputFormat: storageKey` is not supported â€” responses always use `base64`.
- On Windows, `pyautogui` controls the local desktop directly.
