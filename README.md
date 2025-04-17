# SSH Tunnel Manager

SSH Tunnel Manager is a web-based application designed to simplify the management of SSH tunnels, built specifically for use with **Umbrel OS**. This app was created entirely with the assistance of Large Language Models (LLMs) and provides an intuitive interface to configure and monitor SSH tunnels. This is a free alternative for Cloudflare Tunnels. Some advantages include better privacy and better limits (e.g no TOS violations with Jellyfin or 100mb upload limit)

## Features

- Create, edit, and delete SSH tunnels with ease.
- Monitor the status of active tunnels in real-time.
- Simple and clean web interface tailored for Umbrel OS users.
- Deployable via Docker for quick setup.
- Create a SSH key to add it on your remote VPS.

## Screenshots

## Prerequisites
- **Docker** and **Docker Compose** installed.
- Basic knowledge of SSH and networking.

## Installation

Currently, the only supported deployment method is via Docker. Follow these steps to get started:

1. **Clone the Repository**:

   ```bash
   git clone https://github.com/aquaspy/ssh-tunnel-manager.git
   cd ssh-tunnel-manager
   ```

2. **Build and Run with Docker**:

   ```bash
   docker compose build
   docker compose up -d
   ```

3. Access the app in your browser at `http://localhost:port` (port is defined in the Docker Compose configuration, by default the port is 5000).

### Running Without Docker

To run the app directly with Python:

- Modify the paths in `app.py` to match your environment. (By default the path is /data, which requires root)

- Install dependencies:

  ```bash
  pip install -r requirements.txt
  ```

- Run the app:

  ```bash
  python3 app.py
  ```

## Usage

- Navigate to the web interface.
- Add a new SSH tunnel by specifying the local port, remote host, remote port, and SSH server details.
- Monitor and manage active tunnels from the dashboard.
- Stop or delete tunnels as needed.
- TODO: Video of how to use it
- TODO: Guide on how to use it with screenshots

## Contributing

This app was built with LLMs, but contributions from the community are welcome! Feel free to submit issues, feature requests, or pull requests to improve the app.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Notes

- The app is optimized for **Umbrel OS**. Compatibility with other systems may require additional configuration.
- For support or questions, open an issue on this repository.
