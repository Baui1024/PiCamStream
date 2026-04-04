# AI Prompt: PiCam IR Camera Control Panel

Create a single-page web control panel for a Raspberry Pi IR camera.

## Backend

- WebSocket at `ws://<pi-ip>:8082`
- Protocol:
  - Send `{ type: "get" }` to request current settings
  - Send `{ type: "set", data: {...} }` to update settings
  - Send `{ type: "reset" }` to restore defaults
  - Receive `{ type: "settings", data: {...} }` with current values

## Settings to Control

| Setting | Type | Range/Values | Notes |
|---------|------|--------------|-------|
| `ir_mode` | string | `"off"`, `"grayscale"`, `"blue_channel"` | Dropdown select |
| `clahe_enabled` | boolean | true/false | Toggle switch |
| `ae_enable` | boolean | true/false | Auto exposure toggle |
| `exposure_time` | integer | 1000 - 100000 | Microseconds, disabled when ae_enable=true |
| `analogue_gain` | float | 1.0 - 16.0 (step 0.5) | Disabled when ae_enable=true |
| `jpeg_quality` | integer | 1 - 100 | JPEG compression quality |

## UI Requirements

- Dark theme (this is for night vision camera monitoring)
- Single HTML file with embedded CSS/JS (no build step, no external dependencies)
- Show connection status indicator (connected/disconnected/reconnecting)
- Auto-reconnect on WebSocket disconnect with exponential backoff
- Settings update in real-time as sliders move (debounce ~200ms)
- Show current values received from server (syncs across multiple clients)
- "Reset to Defaults" button
- IP address input field at top with connect button (default: `raspberrypi:8082`)
- Mobile-friendly responsive layout

## Styling

- Use CSS custom properties for theming
- Sliders should display their current numeric value
- Group exposure controls visually (exposure_time, analogue_gain) and dim/disable them when auto exposure is enabled
- Clean, minimal design suitable for a monitoring dashboard
- Use monospace font for numeric values

## Example WebSocket Message Flow

```javascript
// On connect, server sends current settings:
{ "type": "settings", "data": { "ir_mode": "grayscale", "clahe_enabled": true, "ae_enable": false, "exposure_time": 30000, "analogue_gain": 4.0, "jpeg_quality": 80 } }

// Client requests setting change:
{ "type": "set", "data": { "exposure_time": 20000 } }

// Server broadcasts updated settings to all clients:
{ "type": "settings", "data": { "ir_mode": "grayscale", "clahe_enabled": true, "ae_enable": false, "exposure_time": 20000, "analogue_gain": 4.0, "jpeg_quality": 80 } }

// Client requests reset:
{ "type": "reset" }

// Server broadcasts default settings:
{ "type": "settings", "data": { ... } }
```
