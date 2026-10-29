# Oracle Cloud Always Free deployment (paper mode only)

This worker only collects read-only market data, monitors a simulated account, and writes research/backtest reports. It does not sign or submit blockchain transactions.

## Why this host

Oracle documents an Always Free Ampere A1 allowance equivalent to up to 2 OCPUs and 12 GB RAM per tenancy in the home region. Creation can fail when free capacity is unavailable; Always Free VMs can also be reclaimed if CPU, network, and memory utilization all remain below Oracle's idle thresholds for a seven-day period. Check the current terms before relying on it:
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm

Do not put paid resources on the account or upgrade the tenancy just to run this worker.

## Prepare a VM

Create an Ubuntu ARM/Ampere A1 VM within the Always Free limits, assign a public IP, and permit SSH (port 22) only from your own IP. This worker does not need any inbound application port.

After connecting to the VM over SSH, install the base packages and create a dedicated unprivileged account. Keep live state outside the code checkout so future code upgrades cannot overwrite it:

\`\`\`bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv
sudo useradd --system --create-home --home-dir /var/lib/meme-bot --shell /usr/sbin/nologin meme-bot
sudo mkdir -p /opt/meme-scanner-bot/releases /var/lib/meme-bot/paper_account
sudo chown -R meme-bot:meme-bot /opt/meme-scanner-bot /var/lib/meme-bot
\`\`\`

After this PR has been merged, clone the main branch. To test the PR before it is merged, replace \`main\` with \`agent/railway-paper-worker\`:

\`\`\`bash
sudo -u meme-bot git clone --depth 1 --branch main https://github.com/amincanani1985-web/meme-scanner-bot.git /opt/meme-scanner-bot/releases/initial
# Seed the persistent directory from the repository without overwriting any existing persistent files.
sudo -u meme-bot cp -an /opt/meme-scanner-bot/releases/initial/paper_account/. /var/lib/meme-bot/paper_account/
sudo -u meme-bot rm -rf /opt/meme-scanner-bot/releases/initial/paper_account
sudo -u meme-bot ln -s /var/lib/meme-bot/paper_account /opt/meme-scanner-bot/releases/initial/paper_account
sudo -u meme-bot ln -sfn /opt/meme-scanner-bot/releases/initial /opt/meme-scanner-bot/current
sudo -u meme-bot python3 -m venv /opt/meme-scanner-bot/venv
sudo -u meme-bot /opt/meme-scanner-bot/venv/bin/pip install -r /opt/meme-scanner-bot/current/requirements.txt
sudo test -f /etc/meme-scanner-bot.env || sudo install -o root -g root -m 600 /dev/null /etc/meme-scanner-bot.env
sudoedit /etc/meme-scanner-bot.env
\`\`\`

Add only the provider variables you actually have to \`/etc/meme-scanner-bot.env\`, such as \`HELIUS_RPC_URL\` and \`HELIUS_API_KEY\`. Do not put credentials or wallet private keys in this repository or in chat. With no Helius credentials, whale-confirmation entries fail closed; keep the worker in paper mode.

## Install and verify the service

```bash
sudo cp /opt/meme-scanner-bot/current/deploy/systemd/meme-paper-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meme-paper-worker
sudo systemctl status meme-paper-worker --no-pager
sudo journalctl -u meme-paper-worker -f
```

The worker monitors positions every 15 seconds, scans every 5 minutes, and refreshes walk-forward reports every 15 minutes. The `current/paper_account` symlink points to `/var/lib/meme-bot/paper_account/`; that is the persistent state and report location on the VM disk.

Back up `paper_account/` before changing code or restoring a VM. A code update should not overwrite the working paper state. Keep SSH restricted, do not expose ports for this worker, and do not add wallet-signing keys. This is a paper/research setup, not a validated profitable trading strategy.
