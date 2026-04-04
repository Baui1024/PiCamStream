sudo apt update -y 
sudo apt upgrade -y
sudo apt install -y libcap-dev python3-dev python3-libcamera python3-kms++
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv venv --system-site-packages