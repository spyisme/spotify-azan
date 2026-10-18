# Spotify Azan Pauser

Automatically pauses active Spotify playback around Cairo prayer times.

- Pauses **1 minute before** each prayer.
- Resumes **2 minutes after** the scheduled prayer time.
- Uses Cairo time (`Africa/Cairo`) and AlAdhan calculation method 5 (Egyptian General Authority of Survey).

Spotify Premium and playback on an active Spotify Connect device are required. Spotify API requests are best-effort and may be delayed.

## Requirements

- Python 3.10 or newer
- A Spotify account with Premium
- A Spotify app Client ID
- For continuous operation: an Ubuntu server with systemd

## Spotify app setup

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Add this redirect URI exactly: `http://127.0.0.1:8888/callback`.
3. Copy the app's Client ID. A client secret is not required; authorization uses PKCE.

## Authorize Spotify on Windows

Clone the public repository and open PowerShell in the project directory:

```powershell
git clone https://github.com/YOUR_NAME/YOUR_REPO.git
cd YOUR_REPO
py -m pip install -r requirements.txt
$env:SPOTIFY_CLIENT_ID="YOUR_CLIENT_ID"
python .\azan.py auth
```

Open the printed URL in a browser on the same PC and approve access. The script saves `tokens.json` beside `azan.py`.

To test pausing and resuming after five seconds, start Spotify playback and run:

```powershell
python .\azan.py test-pause
```

## Deploy on Ubuntu

Replace the example GitHub URL and Spotify Client ID below with your own values.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv

# Create the service account if it does not already exist.
id spotifyazan >/dev/null 2>&1 || sudo useradd --system --home /opt/spotify-azan --shell /usr/sbin/nologin spotifyazan

sudo git clone https://github.com/YOUR_NAME/YOUR_REPO.git /opt/spotify-azan
sudo chown -R root:spotifyazan /opt/spotify-azan
sudo chmod -R g+rX,o-rwx /opt/spotify-azan
sudo install -d -o spotifyazan -g spotifyazan -m 700 /opt/spotify-azan/data

sudo python3 -m venv /opt/spotify-azan/.venv
sudo /opt/spotify-azan/.venv/bin/pip install -r /opt/spotify-azan/requirements.txt

printf 'SPOTIFY_CLIENT_ID=YOUR_CLIENT_ID\n' | sudo tee /opt/spotify-azan/config.env >/dev/null
sudo chown root:spotifyazan /opt/spotify-azan/config.env
sudo chmod 640 /opt/spotify-azan/config.env
```

Transfer the token created on Windows to the VPS. Run this from PowerShell in the project directory, replacing the SSH user and server address:

```powershell
scp .\tokens.json username@server-ip:~/tokens.json
```

Then, on the VPS:

```bash
sudo install -o spotifyazan -g spotifyazan -m 600 ~/tokens.json /opt/spotify-azan/data/tokens.json
rm ~/tokens.json
sudo cp /opt/spotify-azan/spotify-azan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-azan
sudo systemctl status spotify-azan
```

The service runs as the restricted `spotifyazan` account. Code and configuration are under `/opt/spotify-azan`; tokens and runtime data are under `/opt/spotify-azan/data`.

### Service commands

```bash
sudo systemctl status spotify-azan
sudo systemctl restart spotify-azan
sudo systemctl stop spotify-azan
sudo journalctl -u spotify-azan -f
```

Check the calculated prayer times:

```bash
sudo -u spotifyazan env AZAN_STATE_DIR=/opt/spotify-azan/data \
  /opt/spotify-azan/.venv/bin/python /opt/spotify-azan/azan.py times
```

Test pause and resume on the VPS (replace the Client ID and ensure Spotify is playing):

```bash
sudo -u spotifyazan env AZAN_STATE_DIR=/opt/spotify-azan/data \
  SPOTIFY_CLIENT_ID=YOUR_CLIENT_ID \
  /opt/spotify-azan/.venv/bin/python /opt/spotify-azan/azan.py test-pause
```

## Update the VPS

After pushing changes to GitHub:

```bash
sudo systemctl stop spotify-azan
sudo git -C /opt/spotify-azan pull
sudo /opt/spotify-azan/.venv/bin/pip install -r /opt/spotify-azan/requirements.txt
sudo systemctl start spotify-azan
```

## Keep credentials private

- Never commit or upload `tokens.json`, `config.env`, or files in `data/`.
- `.gitignore` excludes credentials, runtime files, and the virtual environment. Check selected files before making the repository public.
- Tokens are not encrypted at rest. Keep the project folder private on Windows; on Linux, the token and data files use restricted permissions.

## Troubleshooting

| Problem | Check |
| --- | --- |
| No token file | Run `python .\azan.py auth` on Windows and transfer `tokens.json` to the VPS. |
| Permission error | Confirm `/opt/spotify-azan/data` is owned by `spotifyazan` and has mode `700`. |
| Spotify is not playing | Start playback on an active, supported Spotify Connect device. |
| Pause or resume failed | Check logs with `sudo journalctl -u spotify-azan -n 50 --no-pager`. Spotify API requests can fail or be delayed. |
| Prayer times differ from a local mosque | Calculation methods and local adjustments can produce different times. |
