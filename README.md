# InvenTree WebSocket Plugin

A WebSocket server plugin for InvenTree inventory management system that enables real-time events and updates.

## Features

- Real-time updates from InvenTree via WebSocket
- Supports part, stock item, and parameter events
- Separate process architecture for stability
- Event filtering
- Enhanced event information

## Installation

### From GitHub

1. Clone this repository:
   ```bash
   git clone https://github.com/benoit505/inventree-websocket-plugin.git
   ```

2. Install into your InvenTree plugins directory:
   ```bash
   cp -r inventree-websocket-plugin/websocket /path/to/your/inventree/plugins/
   ```

### Using pip 

```bash
pip install inventree-websocket-plugin
```

## Configuration

1. Enable the plugin in InvenTree settings
2. Configure the following plugin settings:
   - **Enable Server**: Turn the WebSocket server on/off
   - **Host Address**: Use `0.0.0.0` for Docker installations
   - **Port**: Default is 9020
   - **External URL**: Set to your server's external URL (e.g., `ws://192.168.0.21:9020`)
   - **Enable Event Publishing**: Turn event publishing on/off
   - **Event Types**: Comma-separated list of event types to publish
