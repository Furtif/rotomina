# Rotomina

A web-based management and monitoring tool for Pokemon GO devices with MITM and PlayIntegrityFix support.

![Rotomina Dashboard](https://via.placeholder.com/800x450.png?text=Rotomina+Dashboard)

## Features

- 📱 **Device Management**: Monitor and control multiple Android devices
- 🔄 **Automatic Updates**: Keep Pokemon GO, MITM apps, and PlayIntegrityFix up-to-date
- 💬 **Discord Notifications**: Get alerts for device status and updates
- 📊 **WebSocket Status**: Real-time device status monitoring
- 🛠️ **Remote Management**: Install apps and modules, restart services
- 🔐 **Authentication**: User authentication for secure access

## Requirements

- Python 3.8+
- ADB (Android Debug Bridge)
- Android devices with:
  - Root access
  - Magisk installed
  - PlayIntegrityFix module
  - MITM apps for Pokemon GO

## Installation

### Quick Start (Using Docker Compose)

The easiest way to run Rotomina is using Docker Compose with the pre-built image:

1. Download only the standalone docker-compose.yml file:
```bash
curl -O https://raw.githubusercontent.com/f3ger/rotomina/main/standalone-docker-compose.yml
mv standalone-docker-compose.yml docker-compose.yml
```

2. Create necessary directories:
```bash
mkdir -p data
```

3. Start Rotomina:
```bash
docker-compose up -d
```

The configuration file will be created automatically with default credentials:
- Username: `admin`
- Password: `admin`

**Important:** Change these credentials immediately after your first login.

4. Access the web interface at http://localhost:8000

### Alternative Methods

#### Using Docker Run Command

You can also start Rotomina directly with a Docker run command:

```bash
docker run -d \
  --name rotomina \
  --restart unless-stopped \
  -p 8000:8000 \
  -v $(pwd)/config.json:/app/config.json \
  -v $(pwd)/data:/app/data \
  --privileged \
  ghcr.io/f3ger/rotomina:latest
```

#### Building from Source

If you prefer to build from source:

```bash
# Clone the repository
git clone https://github.com/f3ger/rotomina.git
cd rotomina

# Build and run with Docker Compose
docker-compose build
docker-compose up -d
```

#### Manual Installation

If you prefer a manual installation without Docker:

```bash
# Clone the repository
git clone https://github.com/f3ger/rotomina.git
cd rotomina

# Install requirements
pip install -r requirements.txt

# Start the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Manual Installation

If you prefer a manual installation:

```bash
# Clone the repository
git clone https://github.com/yourusername/rotomina.git
cd rotomina

# Install requirements
pip install -r requirements.txt

# Start the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Configuration

On first run, a default `config.json` will be created. Default login credentials:

- Username: `admin`
- Password: `admin`

**Important**: Change these credentials after first login!

### Device Management

Add devices via the web interface using either:
- IP address (for network devices)
- ADB device ID (for USB connected devices)

## Usage

1. Access the web interface at `http://your-server-ip:8000`
2. Log in with your credentials
3. Add your devices in the Settings page
4. Monitor and manage your devices from the Status page

### Status Page

The Status page shows real-time information about your devices:
- Connection status
- Memory usage
- Installed versions
- Update options

### Discord Notifications

Set up Discord notifications by adding a webhook URL in the Settings page to receive alerts for:
- Device offline/online status
- Memory-based restarts
- Updates and installations

## Updating

When using Docker, update to the latest version with:

```bash
docker-compose pull
docker-compose up -d
```

## Building from Source

If you want to build the Docker image yourself:

```bash
docker build -t rotomina .
```

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- The Pokemon GO community
- MITM developers
- PlayIntegrityFix module developers

## Disclaimer

This project is not affiliated with Niantic or The Pokémon Company. Use at your own risk and responsibility.
