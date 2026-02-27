# Remote-SSH Setup: Polymarket Bot on GCP VM

Your VM details:
- **Name:** polymarket-trading-bot-brazil
- **IP:** 34.95.194.231
- **User:** williamreel07
- **Zone:** southamerica-east1-a
- **Region:** Brazil (southamerica-east1) — good for Polymarket if you had geo issues elsewhere

---

## 1. Configure SSH (choose one)

### Option A: Using gcloud (recommended if you use Google Cloud SDK)

1. Install [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) if needed.
2. Run: `gcloud auth login`
3. Cursor will use `gcloud compute ssh` automatically when you connect.

### Option B: Traditional SSH with a key file

1. Add this to your SSH config:

**Windows path:** `C:\Users\willi\.ssh\config`

```
Host polymarket-brazil
    HostName 34.95.194.231
    User williamreel07
    IdentityFile C:\Users\willi\.ssh\id_rsa
```

2. Replace `IdentityFile` with the path to your **private** key (the one that matches the public key you added in GCP). Common names: `id_rsa`, `id_ecdsa`, `google_compute_engine`.

---

## 2. Connect Cursor via Remote-SSH

1. **Ctrl+Shift+P** → type `Remote-SSH: Connect to Host`
2. If using gcloud: choose **`Connect to Host...`** → enter:
   ```
   williamreel07@34.95.194.231
   ```
3. If using SSH config: choose **`polymarket-brazil`**
4. A new Cursor window opens connected to the VM.
5. **File → Open Folder** → create/open a folder (e.g. `/home/williamreel07/polymarket`).

---

## 3. Get the Project onto the VM

In the Cursor terminal (which runs **on the VM**):

```bash
# If the folder is empty, clone your repo:
cd ~
git clone <YOUR_REPO_URL> polymarket
cd polymarket

# Or if you use a different git URL, adjust accordingly.
# If the repo is private, you'll need to set up SSH keys or a token on the VM.
```

**Alternative:** If your project isn’t in a Git repo, use **File → Open Folder** on the VM and then use **File → Upload...** or drag-and-drop from your local machine (Remote-SSH supports this).

---

## 4. Set Up Python and Dependencies on the VM

In the Cursor terminal (on the VM):

```bash
cd ~/polymarket   # or whatever path you used

# Ubuntu 22.04 has Python 3.10 by default; verify:
python3 --version

# Install pip if needed
sudo apt update
sudo apt install -y python3-pip python3-venv

# Create virtual env (recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 5. Configure .env on the VM

```bash
cp .env.example .env
nano .env   # or use Cursor's editor to edit .env
```

Fill in at least:

- `POLY_PRIVATE_KEY` — wallet private key (never commit this)
- `PROXY_WALLET` — Polymarket profile address
- `PAPER_TRADING=true` — start in paper mode

---

## 6. Run the Bot

```bash
source venv/bin/activate   # if not already active
python main.py
```

To run in the background:

```bash
nohup python main.py > bot_output.log 2>&1 &
```

Or use `tmux` / `screen` to keep it running after you disconnect.

---

## Quick Reference

| Task            | Command / Action                              |
|-----------------|------------------------------------------------|
| Connect Cursor  | Ctrl+Shift+P → Remote-SSH: Connect to Host     |
| Run bot         | `python main.py`                              |
| View logs       | `tail -f bot.log`                             |
| Stop bot        | Ctrl+C or `pkill -f main.py`                  |
