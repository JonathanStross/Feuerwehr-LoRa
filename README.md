# Feuerwehr-LoRa

A gateway for forwarding messages from a Meshtastic LoRa device to a Telegram channel, designed for use by fire departments or similar organizations.

## Features
- Reads messages from a Meshtastic device via serial interface
- Forwards messages to a specified Telegram chat
- Stores all messages in a CSV file for logging
- Configurable via environment variables and Docker Compose

## Requirements
- Python 3.8+
- Docker (recommended for deployment)
- Meshtastic device (e.g., T-Beam)
- Telegram bot and chat

## Setup

### 1. Clone the repository
```sh
git clone <your-repo-url>
cd Feuerwehr-LoRa
```

### 2. Configure environment variables
Edit the `.env` file and set your Telegram bot token and chat ID:
```env
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=your-telegram-chat-id
```

### 3. Prepare data directory
A `data/` directory with a `nachrichten.csv` file is required. This is created automatically, but ensure it exists if running manually.

### 4. Build and run with Docker Compose
```sh
docker-compose up --build
```

### 5. Local development (optional)
Create a virtual environment and install dependencies:
```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment Variables
- `CSV_PATH`: Path to the CSV log file (default: `/data/nachrichten.csv`)
- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token
- `TELEGRAM_CHAT_ID`: The chat ID to send messages to
- `TARGET_CHANNEL_NAME`: Name of the Meshtastic channel (default: Feuerwehr)
- `TARGET_CHANNEL_INDEX`: Channel index (default: 1)
- `MESHTASTIC_DEV`: Serial device path (default: `/dev/ttyUSB0`)
- `DEBUG_PACKETS`: Set to `1` to enable debug output
- `HEALTHCHECK_INTERVAL_SEC`: Healthcheck interval in seconds (default: 3600)

## File Structure
- `main.py`: Main application logic
- `docker-compose.yml`: Docker Compose configuration
- `.env`: Environment variables
- `data/nachrichten.csv`: Message log file

## License
MIT License
