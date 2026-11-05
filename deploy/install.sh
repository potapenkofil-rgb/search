#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-https://github.com/potapenkofil-rgb/search.git}"
BRANCH="${BRANCH:-claude/pensive-volta-nx0nwj}"
DIR="${DIR:-/opt/leadhunter}"
RUN_USER="${RUN_USER:-leadhunter}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти от root: sudo bash deploy/install.sh" >&2
  exit 1
fi

if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
fi

id "$RUN_USER" >/dev/null 2>&1 || useradd -r -m -d "$DIR" -s /usr/sbin/nologin "$RUN_USER"

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  mkdir -p "$DIR"
  git clone -q -b "$BRANCH" "$REPO" "$DIR.tmp"
  cp -a "$DIR.tmp/." "$DIR/"
  rm -rf "$DIR.tmp"
fi

python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

[ -f "$DIR/.env" ] || cp "$DIR/.env.example" "$DIR/.env"
[ -f "$DIR/config.yaml" ] || cp "$DIR/config.example.yaml" "$DIR/config.yaml"
chmod 600 "$DIR/.env"
mkdir -p "$DIR/data"
chown -R "$RUN_USER:$RUN_USER" "$DIR"

cp "$DIR/deploy/leadhunter.service" /etc/systemd/system/leadhunter.service
sed -i "s#/opt/leadhunter#$DIR#g; s#^User=.*#User=$RUN_USER#" /etc/systemd/system/leadhunter.service
systemctl daemon-reload

echo "Установлено в $DIR"
echo "1) Заполни $DIR/.env и $DIR/config.yaml"
echo "2) sudo -u $RUN_USER $DIR/.venv/bin/python -m leadhunter check   (из $DIR)"
echo "3) sudo systemctl enable --now leadhunter && journalctl -u leadhunter -f"
